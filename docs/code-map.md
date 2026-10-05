# Code map — where each piece of logic lives

For developers who want to peek at a specific mechanism: the subsystem →
file(s) table below, with the entry points to start reading from. Subsystems
that have a deeper write-up link to it (`handoffs/` are the design docs).

## Agent loop & guards

| File | What lives here |
|---|---|
| `runtime/loop.py` | The whole agent loop (`AgentRuntime.run()`) — model ↔ tools turns, budgets, compaction trigger, sub-agent spawn, auto-delegate, confirmation routing. ~14 nested closures; read `run()` top to bottom once and the rest of the runtime makes sense. |
| `runtime/run_state.py` | `RunState` — per-run mutable state (extracted from `run()` locals; pure data, no logic). |
| `runtime/turn_guards.py` | Pre/post-turn guard pipeline: stall ladder, budget/context pressure, failure streaks, verify-arm, delegate nudge, wrap-up. Policy lives here. |
| `runtime/final_guards.py` | Final-answer gate: requirements `[must]`, deliverables, verify-the-delegate bounce, truncation, just-reply. Rejections feed `loop_guard.max_rejections`. |
| `runtime/budget.py` | `Budget` + `BudgetExceeded` — iteration/cost/token/wall ceilings, child carve-outs. |
| `runtime/compact.py` + `loop.py::_compact_messages` | Compaction mechanics vs. invocation/pinning — deliberately split. |
| `runtime/verify.py` | `VerifyMixin` — verify bounce, authored checks, fresh-context delegation review. |
| `runtime/selector.py` | Per-run tool-set selection, frozen for prompt-cache stability. |
| `runtime/subcall.py` | RLM primitive: mediated sub-LLM calls over a per-run Unix socket from sandboxed `code.run`. |

Note: `loop_guard` is a **config section**, not a module — its behavior is
split across `loop.py`, `turn_guards.py`, `final_guards.py`.

## Model lifecycle & swapping (the core idea)

| File | What lives here |
|---|---|
| `runtime/process_manager.py` | `ProcessManager` — autostart/stop/monitor/restart of llama-servers and plugin sidecars. |
| `runtime/serving.py` | Serve primitives: launch/stop/wait-healthy, VRAM probes, port picking, litellm register/deregister. |
| `runtime/preset_store.py` | `PresetStore` — presets.db (SQLite), slot assignment (`resolve_slot`/`set_slot`), seeds from `presets/*.conf`. |
| `runtime/serve_preset.py` | Flat KEY=VALUE preset parser (model path, MMPROJ, ALIAS, llama-server flags). |
| `runtime/boot_posture.py` | Serves `models.boot:` presets at startup. |
| `tools/model/catalog.py` | The swap brain: `model.list`/`model.use`, strength registry, `route_strength`, fit-aware eviction planning (`need_shares`/`plan_eviction` — measured per-card VRAM shares, co-tenancy when they fit), live-slot probing. |
| `tools/model/measure.py` | `model.measure` — records a preset's real per-card VRAM + RAM footprint into the catalog (hibernate-all, load, probe, restore) for the fit-aware scheduler. |
| `tools/serve/lifecycle.py` | Agent-facing `serve.*` tools (start/stop/list/status/health). |
| `scripts/start-model.sh` | Universal llama-server launcher (preset-DB mode or `--preset` headless). |
| `scripts/brain-swap.sh` | CLI slot swap via `PUT /api/admin/preset-slots`. |
| `runtime/cloud_store.py` | DB-backed cloud catalog; keys stay env-var names only. |
| `runtime/cloud_gate.py` | Cloud gate — privacy/confirmation enforcement on anything landing on a cloud alias. |
| `tools/llm/cloud_models.py` | `llm.call` — the approval-gated cloud escalation tool. |
| `plugins/jev/` + `plugins/clm/` | Decision-model sidecars (jevify / Contrastive-LM) for fast strength routing; `manage_sidecar` lets the process manager own them. |
| `config/litellm.yaml` | Proxy **seed** — the live catalog is in presets.db and re-rendered to `$JAYNET_DATA/litellm.yaml`. |

Key insight for readers: the loop only ever talks to **stable aliases**
(`local-orchestrator`, `local-specialist`) on the LiteLLM proxy. A swap
changes which llama-server process sits behind the alias — the agent, the
prompt cache and in-flight runs never notice.

## Tools

| File | What lives here |
|---|---|
| `runtime/tool_base.py` | The contract: `Tool`, `ToolResult`, `ToolContext`, path-root confinement, `role_sampling`. |
| `runtime/registry.py` | Auto-discovery of `tools/<ns>/<verb>.py`, custom layer, connector instances. |
| `runtime/confirm.py` | Confirmation providers (web future, TTY fallback). |
| `tools/` | ~40 namespaces, one dir each — `agent/`, `code/`, `fs/`, `git/`, `web/`, `memory/`, `rag/`, `kg/`, `chain/`, `skill/`, `model/`, `serve/`, `specialist/`, `council/`, `mcp/`, `browser/`, `llm/`, `eval/`, … |

Handoff: [../handoffs/tools.md](../handoffs/tools.md). Privacy/taint is
config-driven (`privacy:` in runtime.yaml) with enforcement in
`loop.py` + `cloud_gate.py`.

## Skills, chains, plugins

| File | What lives here |
|---|---|
| `runtime/skills.py` | Skill discovery (frontmatter), layered built-in + custom + plugin skills. |
| `skills/<name>/SKILL.md` | Shipped playbooks; `shape:`/`checkpoints:` turn one into an enforced procedure. |
| `runtime/hooks.py` | Hook registry plugins implement against. |
| `runtime/plugins.py` | Plugin lifecycle: scan/load, hot `enable_live`/`disable_live`, routes registration. Manifest: `plugin.yaml`. |
| `plugins/graphify/` | The reference plugin shape (hooks + routes + tools + skills). |
| `tools/chain/engine.py` | Chain engine — its module docstring is the semantic reference. |
| `runtime/connectors.py` + `tools/connector/` | Connector state + declarative YAML connector builder (`.jayconn`). |
| `runtime/jaypack.py` | `.jaypack` export/import bundles. |

Handoffs: `skills.md`, `chains.md`, `plugins.md`, `connectors.md` in `handoffs/`.

## Web layer

| File | What lives here |
|---|---|
| `web/server.py` | `create_app()` — wiring: runtime, preset overlay, auth middleware, route modules. Plugin routes register **last** (core wins). |
| `web/routes_run.py` | Chat send + SSE stream, slash commands, `/goal`, voice. |
| `web/routes_chats.py` | Saved chats, current chat, flags. |
| `web/routes_admin.py` | `/api/admin/*` — config, presets/slots, backup, HF downloader, services. |
| `web/routes_studio.py` | Studio authoring (skills/chains/tools/evals) + jaypack. |
| `web/routes_eval.py` | `/api/admin/evals/*` + flag→make-test. |
| `web/auth.py` | `UserStore` (users.db), TOTP, sessions, throttling. |
| `web/store.py` | `ChatStore`/`FlagStore`/`ReportStore` (chats.db). |
| `web/goals.py` | Goal supervisor (multi-run `/goal` loop with judge double-check). |
| `web/watchdog.py` | Run coroner — post-mortems for distressed runs. |
| `web/static/app.js` | No-build frontend, all chat/SSE logic. Cache-bust via `?v=N` in `index.html`. |

Handoff: [../handoffs/web-ui.md](../handoffs/web-ui.md).

## Evals

| File | What lives here |
|---|---|
| `runtime/eval_cases.py` | Case YAML schema + layered loading. |
| `runtime/eval_runner.py` | The harness: run_case/run_suite, judge, container sandboxing, expectation checkers, proposals. |
| `runtime/eval_store.py` | eval.db — results, proposals, strength matrix. |
| `runtime/eval_stats.py` | Wilson intervals, McNemar pairing. |
| `runtime/trace.py` | trace.db — per-step trajectory log (eval evidence source). |
| `evals/*.yaml` | Shipped cases + judge calibration. |

Docs: [testing-harness.md](testing-harness.md).

## Config

| File | What lives here |
|---|---|
| `runtime/config_loader.py` | YAML load, path anchoring to JAYNET_HOME/DATA. |
| `runtime/config_schema.py` | Pydantic section validation. |
| `runtime/config_check.py` | Boot-time typo guard. |
| `runtime/config_help.py` + `config/config-help.yaml` | One-line help for the admin Config UI (both directions test-enforced). |
| `runtime/env.py`, `runtime/paths.py` | `JAYNET_*`/`ORCH_*` dual env read, canonical paths. |
| `~/.config/jaynet.env` | Secrets (template: `example_configs/jaynet.env.example`); DBs store env-var *names*, never keys. |

## Memory & state

| Store | Owner |
|---|---|
| trace.db | `runtime/trace.py` |
| eval.db | `runtime/eval_store.py` |
| presets.db (presets, slots, cloud models) | `runtime/preset_store.py`, `runtime/cloud_store.py` |
| chats.db (chats, flags, reports) | `web/store.py` |
| users.db (users, sessions, goals) | `web/auth.py` |
| Persistent agent memory (SQLite FTS5) | `tools/memory/store.py` |
| Working notes | `tools/agent/note.py` |
| Todos (per-run) | `runtime/todos.py` + `tools/agent/todos.py` |
| Projects | `web/projects.py` |
| Scheduler | `runtime/scheduler.py` |
| KG / RAG / research | `tools/kg/graph.py`, `tools/rag/store.py`, `tools/research/loop.py` |

## Scripts

| File | What it is |
|---|---|
| `scripts/setup.sh` | Idempotent installer (venvs, jaynet.env, systemd units). Does NOT build llama.cpp or pull models. |
| `scripts/quickstart.sh` | Minimal try-out: prebuilt pinned llama.cpp + one GGUF. |
| `scripts/pull-model` | HF GGUF pulls over `runtime/hf_pull.py`. |
| `scripts/orch` | CLI driver (runs, traces, `--list-tools`). |
| `scripts/screenshot_pages.py` | Headless console screenshots → `screenshots/`. |
| `scripts/backup.sh` + `runtime/backup.py` | Data-dir backup (systemd timer). |
| Bench/diag | `bench_context.py`, `ctx-cost.py`, `route_bench.py`, `eval-peek.py`, `eval-delta.sh`, `run_timing.py` |
| Live-ops | `slash-run.py` — fires a slash command at the live API, streams the run, auto-approves confirmations (built for long gated ops like `model.measure`) |

## Things that surprise people

- The agent loop is one big file **on purpose** — behavior stayed in
  `loop.py` when state moved to `run_state.py`; guard *policy* is in
  `turn_guards.py`/`final_guards.py` but their thresholds are read from
  config inside `loop.py`.
- Prompts are layered: shipped `prompts/*.md` stay git-pristine; live edits
  land as overlays in `$JAYNET_DATA/custom/` and win while present.
- Cloud-model truth lives in presets.db, not in `config/litellm.yaml`.
- There is no `model.switch` tool — swapping is `model.use`.
