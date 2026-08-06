# Changelog

Breaking changes and release notes. Versions are git tags; the stable API
contract lives in `docs/api.md`, upgrade procedure in `docs/upgrading.md`.

## Unreleased

Behavioural eval harness (Admin → Eval):

- YAML test cases (`evals/` seeds + `$ORCH_DATA/custom/evals/`) run scripted
  or adaptive multi-turn conversations through the real agent loop — the
  unattended toolset, auto-denied confirmations, per-case sandbox — and are
  graded by a judge model (`eval:` config section; cloud alias with
  local-specialist fallback, temperature 0). The only budget is $.
- Results, judge notes and pass-rate trends persist in `eval.db`; failures
  produce deduplicated WHAT/CAUSE/FIX proposals (prompt-tweak lands as a
  git-visible edit of the gate prompt on accept; bug-for-dev writes a
  ready-to-paste issue) — nothing auto-applies.
- Flags grow an "include private context" opt-in (default off) and a
  "make test" button that drafts a case from a flag's coroner report.
- `eval.run` / `eval.list` / `eval.report` tools let the agent self-test;
  cases share via `.jaypack`. 14 seed cases ship in `evals/`.

Install simplification + pre-1.0 cleanup:

- `scripts/setup.sh` (full installer: prereqs, venvs, env file with
  auto-generated secrets, systemd units, linger) and `scripts/quickstart.sh`
  (one-command minimal install: prebuilt llama-server + model download)
- `scripts/orch --doctor` — install validator (10 checks with fix hints);
  `scripts/pull-model` — interactive HuggingFace GGUF downloader
  (`ORCH_MODELS`, default `/srv/models`)
- LiteLLM master key now optional for localhost-only installs (render omits
  it when `LITELLM_MASTER_KEY` is unset)
- runtime.yaml typo guard: boot warns on unknown config sections with
  "did you mean …" hints
- Default model set defined (docs/models.md): fresh installs seed
  brain = Qwen3-4B, embed/rerank = Qwen3 0.6B (all Apache-2.0) — code
  fallbacks, shipped presets and quickstart all point there; existing
  presets.db catalogs are untouched (seed applies to empty DBs only)
- Ports (`ORCH_LITELLM_PORT`, `ORCH_WEB_PORT`) and trusted proxy IP
  (`ORCH_FORWARDED_ALLOW_IPS`) configurable via the env file
- Retired `llama-brain1`/`llama-specialist` units (process manager owns
  models); templates moved to `example_configs/` with `.example` naming;
  version shown in the web UI; `docs/models.md` license-clean model picks

## 0.9.0 — 2026-08-04

First tagged release. Feature-complete daily driver; the 0.9.x line is
contract-hardening toward 1.0 (public open-source release) — see
docs/development.md → Versioning.

Highlights since development started (squashed):

- Web console: multi-user auth (+TOTP 2FA), per-user chats/projects, quick
  settings, run budgets, inline diffs, light/dark theme
- Agent runtime: local-first routing brain + specialist slots, preset
  catalog with GPU/CPU placement, strengths-aware delegation, ~100 tools,
  skills/chains, Studio (admin-created skills/chains/connectors + .jaypack
  share), wiki, memory + KG, trace mining, verify/council/ops tools
- Voice channel `/api/voice` with `voice:false` chat mode for native
  clients; per-user API tokens; SSE streaming; scheduled runs; flags/coroner
- Admin console: status + hardware, processes, presets, prompt, config,
  tools, users, flags, RAG
- Repo hygiene: MIT license, secrets sweep (clean), paths centralized in
  `runtime/paths.py`, nginx example, stable API contract + upgrade guide
