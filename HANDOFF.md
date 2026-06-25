# JayNet Orchestrator — Project Handoff

A self-hosted, local-first LLM **agent** that runs on `wolf` (Arch/CachyOS, dual
AMD Radeon AI PRO R9700 / ROCm). It exposes a tool-using agent loop over a web
chat UI, drives local models through a LiteLLM proxy, and keeps everything —
inference, RAG, research state, file outputs — on the box. This document explains
the whole system: how it's laid out, how a turn actually runs, why a few design
choices are the way they are, and how to deploy and operate it.

---

## 1. The shape of the system

```
┌─────────────┐   HTTP/SSE   ┌────────────────────────────────────────┐
│  web UI     │ ───────────▶ │  web/server.py  (FastAPI + SSE)         │
│ (static/)   │ ◀─────────── │   auth · chats · projects · outputs     │
└─────────────┘   events     │   /api/run → spawns a run task          │
                             └───────────────┬──────────────────────────┘
                                             │ runtime.run(...)
                                             ▼
                              ┌──────────────────────────────┐
                              │  runtime/loop.py  AgentRuntime │  the agent loop
                              │   plan → call model → run tools│
                              │   → repeat → run_finish        │
                              └───────┬───────────────┬────────┘
                                      │               │
                         tool schemas │               │ model calls
                                      ▼               ▼
                          ┌────────────────┐   ┌──────────────────┐
                          │ runtime/       │   │ LiteLLM :4000     │
                          │ registry.py    │   │  ├─ brain  :8090  │ GPU0
                          │ (auto-discovers│   │  ├─ coder  :8080  │ GPU1
                          │  tools/*)      │   │  ├─ embed  :8095  │
                          └───────┬────────┘   │  └─ rerank :8096  │
                                  │            └──────────────────┘
                                  ▼
                          tools/<namespace>/*.py   (75 tools, ~24 namespaces)
                          skills/<name>/SKILL.md    (procedural playbooks)
```

Top-level layout:

- `runtime/` — the engine: `loop.py` (the agent loop + sub-agent seam), `registry.py`
  (tool discovery), `tool_base.py` (the `Tool`/`ToolContext`/`ToolResult` contract),
  `selector.py` (which tools to expose per turn), `budget.py`, `events.py` (the SSE bus),
  `trace.py` (SQLite run log), `outputs.py` (deliverable staging).
- `tools/<namespace>/*.py` — the tools. Each file defines one or more `Tool` subclasses.
- `skills/<name>/SKILL.md` — procedural guides the agent loads on demand (markdown +
  YAML frontmatter). Not code; instructions.
- `web/` — `server.py` (FastAPI), `store.py` (chats/projects SQLite), `static/`
  (the single-page UI: `index.html`, `app.js`, `app.css`).
- `config/runtime.yaml` — the single source of truth for behavior. Also `litellm.yaml`
  (proxy model map) and `qwen3-tools.jinja` (tool-calling chat template).
- `scripts/`, `systemd/`, `lib/` — serving launchers, unit files, shared bash env.
- `data/` — **runtime state, not source**: `rag.db`, `research.db`, chats/users DBs,
  `outputs/`, uploads, projects, `trace.db`, `session.secret`. Excluded from tarballs.
- `tests/` — pytest suite (56 tests).

---

## 2. How a turn runs (the agent loop)

1. The UI POSTs `/api/run`. The server builds the allowed tool list (drops gated /
   remote-only / user-disabled tools), creates a `run_id`, and launches
   `runtime.run(...)` as an asyncio task. The UI opens `GET /api/stream/{run_id}`
   (SSE) for live events.
2. `AgentRuntime.run` (in `runtime/loop.py`) is the loop. Each iteration:
   - emits events through one `emit()` helper (so the **trace DB and the live stream
     never diverge** — every step is logged and streamed from the same call);
   - asks the **brain** model for the next step (prose commentary + tool calls);
   - gates each tool call (allow-list, loop-guard, privacy taint, confirmation),
     then executes (optionally in parallel), appends results to the message list;
   - ticks the budget; optionally compacts old tool results to keep context lean;
   - repeats until the model stops calling tools or a budget/limit/cancel fires.
3. On any exit path — normal, `BudgetExceeded`, `PrivacyViolation`, `CancelledError`,
   or unexpected error — it emits a terminal **`run_finish`** event with the status and
   final answer. The SSE stream breaks on `run_finish`; the UI reverts to idle.

Cancellation: `POST /api/cancel/{run_id}` calls `task.cancel()`. The loop catches
`CancelledError`, sets status `cancelled`, and still emits `run_finish` so the stream
closes cleanly. (The UI also has a safety-net timeout that resets the Stop button to
the logo if that terminal event is delayed by a blocking tool.)

---

## 3. The tool system

**Contract** (`runtime/tool_base.py`): a `Tool` has `name` (`namespace.verb`),
`description`, JSON-schema `parameters`, flags `private` (may the result leave the box
to a remote LLM?) and `requires_confirmation`, and an async `execute(args, ctx) ->
ToolResult`. `ToolContext` carries `config`, `budget`, `request_id`, `owner`, and the
loop-owned **capability seams**: `emit` (stream an event), `spawn` (launch a sub-agent),
`ask_user` (structured questions), and the confirm provider. `needs_confirmation(args,
ctx)` lets a tool decide gating dynamically (e.g. `code.run` only gates when its sandbox
is disabled).

**Discovery** (`runtime/registry.py`): at startup it `rglob`s `tools/**/*.py`, imports
each module, and registers every concrete `Tool` subclass **defined in that module**.
Adding a tool = drop a file in `tools/<ns>/`; no registration step.

**Selection** (`runtime/selector.py`): per turn, modes `all` / `static` / `auto`. In
`auto` it always exposes `core_namespaces` plus any namespace whose keywords match the
user's message (keyword map in `config.tools.keyword_namespaces`). This is the real lever
on per-turn schema-token cost (exposing all 75 tools is ~10k tokens/turn).

**Namespaces** (~24): `web` (search/fetch/render), `browser` (screenshot/pdf), `rag`,
`research`, `code` (run/patch/symbols/tree/deps/delegate), `lint`, `git`, `fs`, `trace`,
`llm`, `serve`, `job`, `gpu`, `kg`, `memory`, `archives`, `arxiv`, `ask`, `deliver`,
`eval`, `test`, `skill`, `agent`. (`mcp` exists but is empty — see §6.)

---

## 4. Why `agent.spawn` logic lives in `runtime/loop.py`

`tools/agent/spawn.py` is intentionally thin: it declares the `agent.spawn` schema,
validates args, and then calls `await ctx.spawn(task, tools=…, model=…, budget=…)`.
**All the real machinery lives in the loop**, as the `ctx.spawn` closure built inside
`AgentRuntime.run`.

That's deliberate, not accidental. Spawning a sub-agent *is* running the agent loop
again, recursively, with:

- a **depth counter** capped by `config.agent.max_depth` (a tool can't see or enforce this),
- a **carved-out child budget** taken from the parent's remaining budget (the loop owns
  the `Budget` object),
- **event emission** (`subagent_start` / `subagent_finish`) on the same seq stream,
- **confirmation / ask-user routing** from the child back up to the parent's UI,
- and the actual recursion entry point, `self.run(...)`.

None of that is available to a tool in isolation — it all belongs to the running loop.
So the loop exposes the capability as `ctx.spawn` and the tool is just a typed front door
to it. This is the same pattern as `ctx.emit`, `ctx.ask_user`, and the confirm provider:
**the loop owns execution mechanics and injects capabilities into tools via context.**
The payoff is that tools stay declarative and portable (testable without a live loop),
while the privileged, stateful orchestration stays in one place. Sub-agents are also a
context-hygiene tool: a child's heavy transcript (e.g. a research sub-question or a
`code.delegate` to the coder) stays in the child's context and never bloats the parent's.

---

## 5. Skills

`skills/<name>/SKILL.md` are procedural playbooks (YAML frontmatter: `name`,
`description`; then markdown). The agent loads one with `skill.load` when its description
matches the task. They encode *how* to use the tools well, not new capabilities. Current
skills include `deep-research`, `local-coding`, and `selftest` (a smoke test that walks
every namespace with the smallest safe input). Skills are loaded at startup and verified
to parse.

---

## 6. Why MCP is empty right now

There is a `tools/mcp/` namespace, but it contains only an empty `__init__.py` — **no MCP
tools and no MCP client is wired in.** The package is a reserved placeholder so that, if
and when we connect to a Model Context Protocol server, the tools land in a namespace
that already exists and routes like the others.

It's empty on purpose: **the orchestrator doesn't currently consume any MCP service.**
Everything it needs is already a first-class local tool (web, rag, code, git, fs, browser,
research, …) talking directly to local processes or the box's filesystem, so there's been
no external MCP server to bridge to. MCP would earn its place the day we want to expose
*someone else's* capability (a third-party MCP server) or expose *our* tools to an external
MCP client — neither of which is in play yet. Until then the namespace stays empty rather
than carrying a stub that pretends to do something. If you see "mcp" listed with zero tools
in a selftest or registry dump, that's expected and correct.

---

## 7. Models & serving

- **Brain** (orchestrator): Qwen3.6-35B-A3B MoE, GPU0, served by llama.cpp on `:8090`,
  131072 ctx. Chosen on VRAM math + agentic-benchmark performance.
- **Coder**: Qwopus3.6-27B-Coder (Q5_K_M, MTP speculative decoding), GPU1, llama.cpp on
  `:8080`, 65536 ctx. Reached via `code.delegate` / `agent.spawn(model="local-coder")`.
- **Embeddings** `:8095` and **reranker** `:8096` — used by `rag.*` and research dedup.
- **LiteLLM** proxy on `:4000` is the single front door (`config/litellm.yaml` maps aliases
  `local-orchestrator`, `local-coder`, embeddings, rerank to the llama.cpp backends, plus
  any cloud models). `LITELLM_MASTER_KEY` guards `:4000`; the llama.cpp backends use
  `api_key: not-needed`.

GPU note (hard-won): launch the coder with **only** `HIP_VISIBLE_DEVICES=1` and
`unset ROCR_VISIBLE_DEVICES` — setting both composes and lands the model on CPU. GPU2 is
the CPU iGPU; ignore it.

---

## 8. The web app

`web/server.py` (FastAPI) serves the static UI and the API. Key pieces:

- **Auth**: cookie session; per-user owner scoping on chats, projects, outputs, disabled
  tools. `account.html` (gold theme) and `admin.html` (red theme, as an "you're in admin"
  cue) are separate pages.
- **Run + stream**: `/api/run` launches the loop task; `/api/stream/{id}` is the SSE feed;
  `/api/cancel|approve|answer/{id}` flow back as plain POSTs. Events ride an in-process
  `EventBus` (`runtime/events.py`) with monotonic `seq` for resumable streams.
- **Chats** (`web/store.py`): per-turn rows store `user_message`, `answer`, `trajectory`,
  and the full **event timeline** (`events` column). The UI keeps only a *slim* transcript
  in localStorage (quota-safe) and rehydrates the full timeline from the server when a saved
  chat is reloaded.
- **Projects**: a chat can bind to a project; files live under the project dir, the agent
  gets project context, and outputs produced during a project turn are swept into it.
- **Outputs / deliverables** (`runtime/outputs.py`): a tool stages files via
  `stage_and_bundle(...)` then `ctx.emit("output", …)`; the UI shows a download chip with an
  inline **preview/open** (`?inline=1` serves a real media type + `Content-Disposition:
  inline`). Editable text opens in an in-app read-only viewer popup; images/PDF open in a
  tab; archives are download-only. Outputs are kept only if the user saves the chat (swept
  otherwise on a TTL).
- **`/api/models`**: reports the loaded orchestrator + coder model with a liveness dot
  (best-effort query to LiteLLM `/v1/models`), shown at the bottom of the chat sidebar.

The UI is gold-themed with the JayNet `#` logo as the brand mark (header, favicon, send
button — the busy state animates the same logo).

---

## 9. Deep research subsystem

`tools/research/*` + the `deep-research` skill implement a disciplined, iterative research
loop (not a one-shot fan-out). The `research.*` tools own durable state in `data/research.db`:
a **frontier** of open sub-questions (with depth + priority), a **visited/dedup set**
(URL + content-hash + **embedding-similarity** semantic dedup so "same facts, different
words" collapse), **budgets** with a novelty-stall stop condition, and **claims with
per-source provenance and a quality score**. The crawl itself reuses `web.search` /
`web.fetch` / `rag.index|search` / `agent.spawn`; findings land in a per-run RAG collection
`research_<run_id>` (retained for follow-ups, delete with `rag.delete`). Division of labor:
the **tool** owns state/dedup/budgets/provenance; the **model** owns relevance judgement,
claim extraction, and contradiction-spotting during synthesis.

---

## 10. Browser tools & the Arch headless fix

`tools/browser/session.py` is the shared headless-browser session for `web.render` and the
`browser.*` tools (`screenshot`, `pdf`). Playwright's **bundled Chromium is an Ubuntu build
that does not run on Arch/CachyOS**, so the session never uses it. It resolves a browser two
ways: (1) connect over CDP to a **containerized Playwright** if `tools.browser.ws_endpoint` /
`BROWSER_WS` is set (preferred for the service — host stays clean, versions matched), else
(2) launch the **system Chromium** binary (`tools.browser.executable_path`, default
`/usr/bin/chromium`, `pacman -S chromium`). It falls back to the bundled build only if no
system binary exists (fine on Ubuntu CI). `web.render` returns page *text* after JS;
`browser.*` return an *image/PDF* delivered as a previewable download.

---

## 11. Deploy & operations

Services run as `systemd --user` units (`systemd/`): `orchestrator-web`, `llama-orchestrator`
(brain), `llama-coder`, the LiteLLM proxy, and the embed/rerank servers. Environment lives in
`~/.config/orchestrator.env` (chmod 600) — it sets `ORCH_HOME/ORCH_CONFIG/PYTHONPATH`, an
explicit `PATH` (user services don't inherit the login PATH; it must reach the venv, git,
firejail, ctags, ruff, uv, `/opt/rocm/bin`), `LITELLM_MASTER_KEY`, and the brain/coder preset
vars. To pick up env changes in an interactive shell: `set -a; source ~/.config/orchestrator.env; set +a`.

Typical deploy: unpack `orchestrator-full.tar.gz` over `/srv/orchestrator` (it excludes
`data/`, venvs, caches — your live DBs are safe), reconcile `config/runtime.yaml` if it
diverged, then restart the relevant unit. Static-only UI changes just need the files copied +
a hard refresh; backend changes need `systemctl --user restart orchestrator-web`.

Python env: venv at `/srv/orchestrator/.venv`; deps split across `requirements*.txt`
(`-web`, `-litellm`, `-test`). `playwright` is installed into that venv; **don't** run
`playwright install-deps` (it calls apt) — pacman provides Chromium's deps.

---

## 12. Testing

`pytest tests/` (run with `PYTHONPATH=. .venv/bin/python -m pytest tests/ -q`). 56 tests cover
the tool discovery contract, code/git/lint/trace tools, the optimizations (compaction,
parallel exec), the research state machine (frontier/dedup/budgets/semantic-dedup/report),
and the browser session resolution + capture delivery (browser mocked — no real Chromium in
CI). Test configs must live inside the repo (e.g. `config/_t.yaml`) because `AgentRuntime`
resolves `tools/` and `prompts/` relative to the config path. The `selftest` skill is the
live, on-box equivalent: ask the orchestrator to run it and it walks every namespace.

---

## 13. Known limitations / future

- **MCP**: empty by design (§6) until there's a server to bridge.
- **Browser version drift**: rolling-release Chromium can outrun Playwright; the CDP-container
  path sidesteps it if that ever bites.
- **Research budget** counts search-steps (sub-questions explored), not raw HTTP calls; a
  sub-agent that runs several searches spends more wall-clock than the counter implies.
- **Semantic dedup** signature is the page's first ~2000 chars — great for "same article,"
  weaker for pages that only diverge deep in the body (`dedup_prefix_chars` is tunable).
- **localStorage** keeps a slim transcript only; the full event timeline of an *unsaved* chat
  is not preserved across a refresh by design — save the chat to keep it.

---

*This handoff describes the whole project as of the current tree. The older
`handoff.md` was a single-session deploy note and can be archived/removed (see the cleanup
commands provided alongside this document).*
