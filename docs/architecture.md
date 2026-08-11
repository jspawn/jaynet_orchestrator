# Architecture & layout

## Architecture

```
browser / voice client
        │  HTTP + SSE
  web/server.py ──────────────┐
        │                     │ admin page, flags, watchdog reports
  runtime/loop.py (agent loop: model ↔ tools, budgets, compaction, trace)
        │
  tools/ (auto-discovered)  skills/ (prompt playbooks, loaded on demand)
        │
  LiteLLM proxy (:4000) ── local llama-servers (brain :8090, specialist :8080,
        │                  embed :8095, rerank :8096 — launched by the
        │                  process manager from the preset catalog)
        └─ cloud: Kimi, Qwen, Gemini, GLM (via llm.call, approval-gated)
```

A run: `POST /api/chat` → slash commands routed (`/goal`, `/compact`, `/imp`,
`/wgs`) or the agent loop starts. Tool selection ships ~16 core tools and adds
namespaces by keyword (`tool_selection` in runtime.yaml); the set is frozen
per run. State-changing and cloud calls pause for human approval. Every step
is traced to `trace.db` and streamed to the UI over SSE.

## Layout

| Path | What |
|---|---|
| `web/` | FastAPI server, auth, chats/users/goals/projects stores, watchdog, static UI |
| `runtime/` | agent loop, tool registry/selector, budgets, compaction, scheduler, preset store |
| `tools/` | tool implementations, one namespace per dir (fs, code, git, web, rag, llm, …) |
| `skills/` | SKILL.md playbooks the model loads via `skill.load` |
| `chains/` | named multi-step pipelines the model runs via `chain.run` |
| `prompts/` | `orchestrator-gate.md` — the shipped system prompt (~850 tok); live edits write an overlay in `$JAYNET_DATA/custom/` that wins while present |
| `config/` | `runtime.yaml` (main config), `litellm.yaml` (proxy config SEED — rendered to `$JAYNET_DATA/litellm.yaml`), `quick-replies.yaml`, chat templates |
| `presets/` | factory llama-server presets (seed the DB catalog; edit via admin UI afterwards) |
| `scripts/` | `orch` CLI, `start-model.sh` (preset launcher), installers, dev benchmarks |
| `systemd/` | user units (installed verbatim via `cp`) |
| `example_configs/` | adapt-and-install templates: `jaynet.env.example` (secrets/paths/ports), `nginx.conf.example` (reverse proxy) |
| `docs/` | everything you're reading |
| `tests/` | pytest suite (~1100 tests, no network) |

## Notable subsystems

- **Goal mode** (`/goal`) — user-bound objective pursued one turn per run by
  the supervisor in `web/goals.py`, with ceilings and a completion judge.
- **Watchdog** (`web/watchdog.py`) — postmortem on stuck/failed runs; writes a
  capped, deduped report visible in the admin Flags tab.
- **Process manager** (`runtime/process_manager.py`) — launches/stops
  llama-servers from the preset catalog inside the web unit's cgroup; replaces
  systemd units for models.
- **RAG** (`tools/rag/`) — embed (:8095) + optional rerank (:8096), direct
  HTTP, not via LiteLLM.
- **Scheduler** (`runtime/scheduler.py`) — `schedule.*` tools + web tick fire
  recurring/one-shot runs.
- **Chains** (`tools/chain/`, `chains/*.yaml`) — named, reusable multi-step
  pipelines: sequential sub-agent + local prompt steps wired with
  `{{input}}` / `{{steps.<id>.output}}` placeholders, run via `chain.run`.
  Prompt steps are local-only so a chain can never bypass the cloud
  privacy/approval gate.
- **MCP bridge** (`tools/mcp/`) — `mcp.list`/`mcp.call` connect to Model
  Context Protocol servers (stdio subprocesses or HTTP endpoints) from
  `tools.mcp.servers` in runtime.yaml. Confirmation-gated per call by default,
  results private, stdio env scrubbed of secrets. Needs the optional `mcp`
  package (requirements-tools.txt).
- **Studio** (admin tab, `web/routes_studio.py`) — the admin creates custom
  skills, chains, API connectors and python tools in the browser, with
  AI-assisted drafting (local model guided by the writing-great-skills skill).
  Custom artifacts live in `$JAYNET_DATA/custom/{skills,chains,connectors,tools}`
  and are layered over the built-ins (custom wins on name clash; survives
  git-pull deploys). Connectors are declarative YAML HTTP tools — no code,
  credentials only as env-var references. Python tools run with orchestrator
  privileges (admin-trusted) and take effect on restart. Everything is
  exportable/importable as `.jaypack` zips (`runtime/jaypack.py`) for sharing
  between JayNet installs.
- **Harness todo list** (`runtime/todos.py`, `tools/agent/todos.py`) — the
  agent's structured plan for multi-step work, rendered live in the web UI's
  collapsible ToDos side panel. One `todos` tool manages the per-run list
  (set/update/add/remove/clear; statuses pending/working/done/failed/skipped,
  at most one working). Every change emits a full-snapshot `todos` SSE event;
  the loop re-injects a compact rendering each turn (when the working anchor
  is off, at the `agent.anchor.todos_reinject` placement — trailing default)
  so the list survives compaction. The architect's UNITS become the list
  automatically; only a `todos_sync` child (the architect's executor) drives
  the parent's panel and state — a plain sub-agent's list stays its own.
- **Coding flow** (`runtime/context_pack.py`, `tools/code/delegate.py`,
  `tools/agent/architect.py`) — coding sub-agents start oriented: a
  char-budgeted repo map (one line per source file, cached on a tree
  fingerprint) plus the workspace's `JAYNET.md`/`AGENTS.md`/`CLAUDE.md` are
  prepended to delegate/architect spawns (`tools.code.repomap`). The verify
  gate runs its check ONCE before the agent starts — a final failure
  identical to that pre-existing baseline counts as "not worse", so
  pre-existing red is never chased or blamed on the change.
  `code.delegate isolated:true` runs the coder in a throwaway git worktree
  (`.jaynet-worktrees/`, own branch, spawn `work_root_path` confined to the
  parent's roots); the live tree stays untouched and the diff is reviewed /
  merged / discarded afterwards with the confirmation-gated git tools. The
  architect's UNITS carry `| check: <command>`; when every unit has one
  (`architect.per_unit_verify`), each unit executes as its own spawn gated
  mechanically on that check, stopping at the first failure, instead of one
  executor asked politely to self-check.
