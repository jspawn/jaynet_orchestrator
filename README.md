# Orchestrator

Local-first LLM orchestrator for a dual-GPU workstation. A FastAPI web console
drives an agent loop over local llama-server models, with cloud models as
escalation only (local first; cloud when local can't solve it). Multi-user
(login + 2FA), per-user chats and projects, ~100 tools, on-demand skills.

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
        │                  process manager from presets/)
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
| `runtime/` | agent loop, tool registry/selector, budgets, compaction, scheduler, skills loader |
| `tools/` | tool implementations, one namespace per dir (fs, code, git, web, rag, llm, …) |
| `skills/` | SKILL.md playbooks the model loads via `skill.load` |
| `prompts/` | `orchestrator-gate.md` — the live system prompt (~850 tok) |
| `config/` | `runtime.yaml` (main config), `litellm.yaml` (model routing), `quick-replies.yaml`, chat templates |
| `presets/` | llama-server launch presets (one .conf per model) |
| `scripts/` | `orch` CLI, `start-model.sh` (preset launcher), dev benchmarks |
| `systemd/` | user units + `orchestrator.env` template |
| `docs/` | testing-harness guide |
| `tests/` | pytest suite (~600 tests, no network) |

## Configuration

- **`config/runtime.yaml`** — everything: system prompt, budgets, tool
  selection, privacy/confirmation gates, model catalog + GPU posture, process
  manager, goal mode, watchdog, voice, per-tool settings. Admin page edits
  persist as DB overrides applied live.
- **`config/litellm.yaml`** — LiteLLM model list: local servers + cloud
  providers behind OpenAI-compatible aliases.
- **Secrets** — `~/.config/orchestrator.env` on the host (template:
  `systemd/orchestrator.env`). Never commit it.
- **Data** — lives outside the repo (`/srv/data`): chats.db, users.db,
  trace.db, rag.db, uploads, outputs, projects.

## Notable subsystems

- **Goal mode** (`/goal`) — user-bound objective pursued one turn per run by
  the supervisor in `web/goals.py`, with ceilings and a completion judge.
- **Watchdog** (`web/watchdog.py`) — postmortem on stuck/failed runs; writes a
  capped, deduped report visible in the admin Flags tab.
- **Process manager** (`runtime/process_manager.py`) — launches/stops
  llama-servers from `presets/` inside the web unit's cgroup; replaces systemd
  units for models.
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
