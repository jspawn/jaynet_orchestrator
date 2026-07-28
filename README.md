# Orchestrator

Local-first LLM orchestrator for a dual-GPU workstation. A FastAPI web console
drives an agent loop over local llama-server models; cloud models are
escalation only (local first — cloud when local can't solve it). Multi-user
(login + TOTP 2FA), per-user chats and projects, ~100 tools, on-demand skills,
voice channel, scheduled runs, and an admin console for prompts, budgets,
tools, users, flags and the model catalog.

## What's special

- **Multi local models, one brain.** The orchestrator model (GPU 0) reasons,
  plans and routes; a *specialist* slot (GPU 1) holds a swappable second model
  (coding, research, security, …) that heavy sub-tasks are delegated to via
  `code.delegate` / `agent.spawn`. CPU-only embed + rerank servers back the
  RAG tools. All of them run side by side behind one LiteLLM proxy.
- **The model switcher.** A curated preset catalog describes every servable
  model (weights, port, GPU, VRAM, strengths). `model.use('<name>', swap: true)`
  stops the current GPU-1 model and boots another one in place — the brain
  does this itself mid-chat when a task calls for a different specialist.
  Static ports + LiteLLM aliases mean no proxy re-registration; `served_id`
  mismatch checks catch a wrong model on a slot.
- **Strengths-aware routing.** Each preset carries capability tags
  (`strengths: [coding, research, …]`) that are injected into the brain's
  system prompt, so it knows *which* specialist is live and won't send a
  coding task to a research model.
- **Admin-managed catalog.** Presets live in a SQLite DB
  (`/srv/data/presets.db`, seeded once from `runtime.yaml`) and are edited in
  the admin UI — add models, retune launch flags, reassign slots, no restarts
  of the web service.
- **Local-first with guardrails.** Cloud calls (llm.call) are approval-gated
  and privacy-aware (private tool results never leave the box without
  consent); budgets cap iterations/tokens/cost per run; every step is traced.

## Setup

Assumptions: Linux, systemd `--user` services, AMD GPUs via ROCm (NVIDIA/CPU
works too — adjust the presets and the llama.cpp build). Install root is
`/srv/orchestrator`, data lives in `/srv/data`.

1. **llama.cpp.** Build `llama-server` with GPU support; the presets expect it
   at `/srv/llama/llama.cpp-rocm/build/bin/llama-server` (override with
   `LLAMA_BIN`). `tools.serve` sources `/srv/llama/rdna4-env.sh` before
   launches (ROCm env). The service user must be in the `video` + `render`
   groups.
2. **Models.** Download GGUFs under `/srv/models/…` and point the presets at
   them. The shipped catalog: brain = Hermes3.6-35B-A3B (GPU 0), specialist =
   Fable-27B (GPU 1, swappable: tess/ornith/agents1/dolphin), embed + rerank
   (CPU). Adjust `presets/*.conf` to your hardware (ctx size, KV quant, VRAM).
3. **Python envs.**
   ```
   python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-web.txt
   python -m venv litellmenv && litellmenv/bin/pip install -r requirements-litellm.txt
   .venv/bin/pip install -r requirements-test.txt   # dev only
   ```
4. **Env file (secrets + paths).**
   ```
   install -Dm600 systemd/orchestrator.env ~/.config/orchestrator.env
   # edit: ORCH_SESSION_SECRET, ORCH_WEB_TOKEN, LITELLM_MASTER_KEY,
   # first-boot ORCH_ADMIN_USER/PASSWORD, cloud keys (optional)
   ```
5. **systemd user units.**
   ```
   mkdir -p ~/.config/systemd/user
   cp systemd/*.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now litellm-proxy orchestrator-web
   loginctl enable-linger "$USER"   # keep services up without a login session
   ```
   The web service's process manager boots the models itself (`processes:` in
   `runtime.yaml`). The `llama-*.service` units are headless fallbacks — keep
   them disabled while orchestrator-web runs (port fight).
6. **First run.** Browse to `http://<host>:8071`, log in with the seeded
   admin, then remove `ORCH_ADMIN_*` from the env file. The preset catalog
   self-seeds into `/srv/data/presets.db`; manage it in **Admin → Presets**.
   Check **Admin → Status** for service health.

Optional pieces: SearXNG container for `web.search` (`tools.web.search_endpoint`),
system Chromium or a Playwright CDP container for `browser.*`, `firejail` for
the code sandbox, cloud API keys for `llm.call` escalation.

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
| `prompts/` | `orchestrator-gate.md` — the live system prompt (~850 tok) |
| `config/` | `runtime.yaml` (main config), `litellm.yaml` (model routing), `quick-replies.yaml`, chat templates |
| `presets/` | factory llama-server presets (seed the DB catalog; edit via admin UI afterwards) |
| `scripts/` | `orch` CLI, `start-model.sh` (preset launcher), dev benchmarks |
| `systemd/` | user units + `orchestrator.env` template |
| `docs/` | testing-harness guide |
| `tests/` | pytest suite (~630 tests, no network) |

## Configuration

- **`config/runtime.yaml`** — everything: system prompt, budgets, tool
  selection, privacy/confirmation gates, GPU posture, process manager, goal
  mode, watchdog, voice, per-tool settings. Admin page edits persist as DB
  overrides applied live.
- **Preset catalog** — `/srv/data/presets.db` (admin → Presets). The
  `models.presets` block in runtime.yaml is the factory seed only.
- **`config/litellm.yaml`** — LiteLLM model list: local servers + cloud
  providers behind OpenAI-compatible aliases.
- **Secrets** — `~/.config/orchestrator.env` on the host (template:
  `systemd/orchestrator.env`). Never commit it.
- **Data** — lives outside the repo (`/srv/data`): chats.db, users.db,
  trace.db, presets.db, rag.db, uploads, outputs, projects.

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

## Development

Dev checkout: `/srv/orch-dev`. Live install: `/srv/orchestrator` — never edit
live directly; deploy = git pull + `systemctl --user restart`.

Run the suite from the dev checkout with the live venv:

```
cd /srv/orch-dev && /srv/orchestrator/.venv/bin/python -m pytest tests/ -q
```

Conventions: no cross-test imports (copy helpers), monkeypatch instead of
network, comments short and plain. See `docs/testing-harness.md` for the
`test.run` harness and `ToDos_for_later.md` for parked ideas.
