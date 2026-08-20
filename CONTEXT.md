# CONTEXT.md — code-facing glossary for AI-assisted dev sessions

Read this before making changes. It maps the domain terms to where they live
in the code, so edits land in the right layer. User-facing vocabulary (what
the admin console calls things) lives in `docs/glossary.md` — keep both in
sync when a term changes meaning; do not duplicate prose.

## Layers (respect them — this is the architecture's main rule)

- `runtime/` — the harness core. No web imports, no FastAPI. Everything
  web-facing is injected (`on_event`, `confirm_provider`, hooks like
  `eval_runner.set_disabled_hook`).
- `web/` — FastAPI + SSE. `web/server.py:create_app` builds the shared state
  namespace (`s`) and registers `web/routes_*.py` in a fixed order.
- `tools/` — plugin-discovered tool implementations (`<ns>/<name>.py` →
  `ns.name`). Tools only see a `ToolContext`, never the runtime.
- `skills/` — markdown instruction packs the model loads mid-run via
  `skill.load`. Not code.
- `evals/` — behavioural test cases (YAML) played through the real loop.

## Runs & the loop

- **Run** — one `AgentRuntime.run(...)` call (`runtime/loop.py`). Identified
  by `run_id`; events stream through `EventBus` to SSE.
- **Iteration** — one model turn inside a run. Budgets (`runtime/budget.py`)
  cap iterations / wall clock / cost / tokens; **0 = unlimited** everywhere.
- **Brain** — the model driving the loop (`runtime.model`, default alias
  `local-orchestrator`). `/imp` sets a per-user override.
- **Specialist** — secondary model the brain calls via `llm.call` /
  `agent.spawn` (alias `local-specialist`).
- **Alias** — LiteLLM model name; resolved via
  `tools/llm/cloud_models.py:resolve_model_alias`. Code never passes file
  paths to models.

## Models & serving

- **Preset** — a model description in the DB catalog
  (`runtime/preset_store.py`): alias, port, GPU, `.conf` (GGUF + llama-server
  flags) or a **remote_host** (an adopted LAN server — probe only, never
  launched).
- **Boot slot** — `config.models.slots`: `brain`, `specialist`,
  `specialist2`, `specialist3`, `embed`, `rerank`; empty string = slot
  disabled. Process names mirror slot names.
- **Boot posture** — `runtime/boot_posture.py`: at startup, make the served
  models match the configured slots (via `model.use`), never fatal.
- **Managed processes** — `runtime/process_manager.py`, wired in
  `web/routes_procs.py`. Startup/shutdown hooks are appended to
  `s.startup_hooks`/`s.shutdown_hooks` and run by the lifespan in
  `create_app` (no `app.on_event` — deprecated).

## Confinement & privacy (do not loosen without an explicit ask)

- **work_root** — the only directory `fs.*`/`code.*` tools may touch. A
  project's files dir when a project is active, else the per-chat scratch
  (`_scratch_root` in `web/server.py`), else a per-run sandbox.
- **Taint** — `share_private=False` + private tool output marks a
  conversation tainted; cloud models are blocked while tainted
  (`runtime/cloud_gate.py`, `config.privacy.remote_llm_tools`).
- **Confirmation gate** — tools with `requires_confirmation` need the
  provider's approval (`runtime/confirm.py`); web runs use the Web provider,
  unattended/eval runs auto-deny (except sandbox-confined `fs.write/fs.edit`).

## Persistence (all under `$ORCH_DATA`, never git-managed)

- `trace.db` — every run's events (operational, retention-pruned).
- `eval.db` — eval results + improvement proposals, long-term
  (`runtime/eval_store.py`). `brain` labels a run's model/variant;
  `benchmark=1` marks benchmark reps (excluded from default statistics).
- `chats.db` (`web/store.py`) — chats, flags, coroner reports.
- `users.db` (`web/auth.py`) — users, API tokens, config overrides
  (admin-persisted `runtime.yaml` overrides layered at boot).

## Customization layers (user data shadows shipped defaults)

- **Custom layer** — `$ORCH_DATA/custom/`: skills, evals, chains, tool
  description overrides. Custom wins on id clash; builtins stay pristine.
- **Gate prompt** — the system prompt. Shipped at
  `prompts/orchestrator-gate.md`; a live overlay shadows it at
  `$ORCH_DATA/custom/<same-name>` (`runtime/gate_prompt.py`). Accepted eval
  proposals append dated bullets under `<!-- eval-proposals -->`.
- **jaypack** — `runtime/jaypack.py`: export/import bundle for
  skills/chains/evals (Studio tab).

## Plugins (optional capability bundles)

- **Plugin** — a directory with `plugin.yaml` under `plugins/` (repo
  builtins, default disabled) or `$ORCH_DATA/plugins/` (installed, default
  enabled; same name shadows builtin). Loaded at startup by
  `runtime/plugins.py`; disabled or missing-dep plugins are never imported.
  Toggle via admin → Plugins (persisted as a config override, needs restart).
- **Hook** — `runtime/hooks.py`: the ONLY core↔plugin seam. Plugins provide
  functions named after `HOOK_NAMES` (`augment_project_context`,
  `on_project_delete`, `on_project_file_changed`); core fires them wrapped in
  try/except. A plugin never imports `web/*`.
- **Graph (per project)** — the graphify plugin (`plugins/graphify/`):
  wraps the graphify CLI over a project's `files/`, output at
  `<project>/graphify-out/` (dies with the project). `graph.*` tools are
  `private=True`; the semantic pass goes through `plugins.graphify.model`
  (local alias by default).

## Eval harness

- **Case / suite / benchmark** — `runtime/eval_cases.py` (YAML schema),
  `eval_runner.run_suite` (sequential, cost-capped, `should_stop` = admin
  cancel), benchmark = same suite × N variants (model + sampling) recorded
  under each variant's label.
- **Judge / driver** — one-shot cloud calls in `eval_runner` (fallback
  `local-specialist`); judge grades + classifies failures into proposals.
  Nothing auto-applies — proposals wait for an admin accept in
  `web/routes_eval.py`.

## Conventions

- Tests: `tests/`, run `.venv/bin/python -m pytest tests/ -q`. Hermetic —
  they monkeypatch `runtime.paths` to tmp dirs; never touch real `$ORCH_DATA`.
- Comments carry audit references (`audit B5`, `S2`, …) — keep them; they
  record why a guard exists.
- Env vars: `ORCH_*` (legacy prefix kept for compatibility) + `JAYNET_*`,
  loaded from `~/.config/jaynet.env` (`runtime/env.py:load_env_file`,
  `setdefault` — real env always wins). CLI entry points (`scripts/orch`)
  self-bootstrap: re-exec into the checkout's `.venv` and load that env file
  before importing runtime modules.
