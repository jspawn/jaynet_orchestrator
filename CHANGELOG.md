# Changelog

Breaking changes and release notes. Versions are git tags; the stable API
contract lives in `docs/api.md`, upgrade procedure in `docs/upgrading.md`.

## Unreleased

- **setup.sh pins LiteLLM from `requirements-litellm.lock`** (was the loose
  `.txt`): a fresh install no longer resolves a too-new FastAPI that breaks
  the proxy's imports, and re-running setup heals a drifted litellmenv. The
  lock's uvloop is bumped to 0.22.1 (0.21 doesn't import on Python 3.14).

## 0.9.3 — 2026-08-11

- **HF downloader: chat templates + wired preset suggestions.** Repo
  listing includes `.jinja` chat templates (marked "template" in the UI);
  `create preset` now detects a sibling `mmproj*.gguf` / `.jinja` in the
  same repo and prefills `MMPROJ`+`MMPROJ_OFFLOAD` / `TOOLS_TEMPLATE` in the
  suggested .conf, with a note when the referenced sibling isn't downloaded
  yet.
- **Adopt any OpenAI-compatible server as a remote preset (vLLM, Ollama).**
  Remote presets now accept full endpoint URLs (`http://vllm-box:8000`,
  scheme defaults work) and carry a `backend` label (llama/vllm/ollama/openai)
  plus per-preset `caps` overrides (vision/thinking). Probing matches
  `served_id` across all models a multi-model server reports; the jinja
  thinking switch and vision gating follow backend+caps; keyed endpoints
  (401/403) are reported as "authentication required" — adopted endpoints
  must be keyless for now. Admin → Presets gains endpoint/backend/caps
  fields; existing presets DBs migrate on next start. See
  [docs/models.md](docs/models.md#adopt-existing-server).
- **setup.sh robustness**: systemd unit and env-file path rewrites work from
  any clone directory (were hardcoded to /srv/orchestrator, dying with
  203/EXEC); the first-login credentials (admin + generated password) are
  always printed at the end of setup.
- **Docs**: install guide split into setup_installation.md (scripted) and
  manual_installation.md (by hand); new glossary; manual guide's
  helper_scripts section refreshed (backend menu, no removed scripts).
- **Docs audit fixes**: preset-key table completed to the launcher's real
  vocabulary (MMPROJ/MTP/reasoning/embed keys); remote-preset docs brought
  post-Layer-1 (model-placement, admin); stale live-install references
  removed (testing, development); paths/backup commands corrected
  (manual_installation, upgrading); `/api/voice` config gate documented.

## 0.9.2 — 2026-08-11

- **Quick start: one command to run, stranger-proof prompts.**
  `scripts/quickstart.sh` now writes a `start.sh` that runs the model and
  the web app in a single terminal (Ctrl+C stops both; the exit trap takes
  the model down). Ports are asked interactively (defaults `4000`/`8071`,
  `JAYNET_LITELLM_PORT` / `JAYNET_WEB_PORT` win) with re-ask on taken or
  invalid input — a custom model port is also written into
  `config/runtime.yaml` (`orchestrator.litellm_base`), since quickstart
  runs no LiteLLM proxy. A `ldd` check catches missing shared libraries
  (e.g. `libgomp` on stock Ubuntu/WSL) with the exact apt/pacman package
  hint instead of a raw linker error at first start. `start.sh` re-checks
  its ports and fails with a friendly hint (SO_REUSEADDR probes — no
  TIME_WAIT false positives on quick restarts). All script entry points
  use `python3` shebangs now (stock Ubuntu has no `python`).
- **Quick start default model is Qwen3-1.7B** (was Qwen3-4B): ~1.3 GB,
  2–3× faster on CPU, same family/template with tool calling intact —
  the 4B stays the preset-seed brain for full/GPU installs and is one
  explicit `scripts/quickstart.sh Qwen/Qwen3-4B-GGUF` away.
- **Bare `test` as a first message is a smoke test, not an agent run.**
  The classic first thing a new user types is intercepted in `/api/chat`
  (bare `test` only — no attachments, no history, no project) and answered
  with a liveness probe of the model endpoint: "Smoke test passed/failed"
  with the served model id and a pointer to `start.sh` / Admin → Status.
  In a project, `test` still means "run the tests"; longer messages reach
  the loop as before. The probe sends `LITELLM_MASTER_KEY` when set.
- **README: install-from-scratch pass.** Prerequisite commands for Arch +
  Ubuntu/Debian (incl. `uv`, `libgomp1`), WSL2 note for Windows, the quick
  start framed as a throwaway try-out with a cleanup block, `setup.sh` as
  the fixed install, first-login documents the seeded `admin` user with
  the one-time generated password, and the repo moved to
  `github.com/jspawn/jaynet_orchestrator`.
- **Handoffs for AI-assisted modification** (`handoffs/`): self-contained
  briefings to paste into a fresh AI session — re-theme/replace the web UI,
  create skills, create chains, add tools (Python/connector/MCP) — plus a
  shared ground-rules index (tests, custom layer vs repo, conventions).
- **Remote slots: Stop is guarded too.** Admin → Processes refused
  start/restart on remote slots already; `stop` now returns the same 409
  ("served by \<host\>, probe only") instead of a misleading success.
- **Preset seeds are clone-location independent.** The shipped seed entries
  in `config/runtime.yaml` now use `presets/...` paths relative to
  `JAYNET_HOME` (was absolute `/srv/orchestrator/...`), so a fresh install
  anywhere seeds its preset catalog from the files that ship in the repo.

## 0.9.1 — 2026-08-10

- **Remote presets: local models served by another LAN box.** A preset with
  a `remote_host` (Admin → Presets → *remote* checkbox) is a llama-server
  running elsewhere in the homelab, treated like a local preset — boot
  slots, `model.use`, `model.list`, `local-*` aliases — except JayNet never
  launches/swaps/stops it: the process manager skips remote slots at boot
  (*remote — probe only* on the Processes tab), `serve.start` and
  `start-model.sh` refuse them, and `model.use` only health-probes. Stays
  out of cloud models, so the privacy gate keeps classifying it as local;
  no cost, no key. Plain HTTP on the LAN — see
  [docs/model-placement.md](docs/model-placement.md).

- **Boot slots can be empty; up to three specialists.** Every slot except
  brain can be set to **(none)** (Admin → Presets → Boot model slots) to
  run without that process — skipped at startup, shown as *disabled (slot
  empty)*, manual start refused. An empty specialist keeps its LiteLLM
  alias alive by following the brain. New optional `specialist2` /
  `specialist3` slots (ship empty; new dormant `processes:` entries in
  runtime.yaml) render as `local-specialist2` / `local-specialist3`
  aliases while assigned.

- **ToDos panel: floating tab/card on all viewports.** Collapses to a small
  status tab (JayNet-logo pip: pulsing while working, goldenrod pending,
  red failed, green all done) pinned inside the chat area on desktop and
  mobile; expands to the full step list in place. ToDos clear on the next
  prompt after a run finishes.

- **Rebrand: orchestrator → jaynet in deployment-facing names.** The env
  file moves to `~/.config/jaynet.env` (template
  `example_configs/jaynet.env.example`), the web unit to
  `jaynet-web.service`, and every env var to the `JAYNET_*` prefix.
  **Not breaking for Python**: `runtime/env.py` dual-reads —
  `JAYNET_*` wins, `ORCH_*` still works everywhere in app code, scripts and
  `start-model.sh`. **Breaking for systemd**: the units substitute
  `${JAYNET_*}` from the env file directly, so switching units requires the
  renamed env file — migration steps in `docs/upgrading.md`
  ("Renamed in 0.9.x"). Kept as-is on purpose: the `local-orchestrator`
  LiteLLM alias (fallback chains), the `scripts/orch` CLI, and the internal
  `ORCH_EXEC_OUT` snippet contract.

- **HuggingFace downloader in Admin → Presets**: repo → .gguf file picker
  with sizes, background downloads with live progress + cancel, then
  "create preset" opens the editor prefilled (name, alias, next free port,
  .conf skeleton with `MODEL_PATH`, VRAM estimate). New shared core
  `runtime/hf_pull.py`; `scripts/pull-model` keeps its CLI contract on top
  of it. API: `/api/admin/hf/{files,download,jobs,cancel,preset-suggestion}`.
  `HF_TOKEN` in the service env authenticates both paths (gated repos,
  rate limits); stale `.part` residue is swept from the models dir on
  startup. The env template also drops inline comments — systemd keeps
  them as part of the value.

- **Styled dialogs everywhere** (GUI audit C4): new `web/static/dialog.js` —
  promise-based `dlgAlert`/`dlgConfirm`/`dlgPrompt`, themed via CSS
  variables, Esc/Enter/click-outside — replaces every native
  `alert()`/`confirm()`/`prompt()` across chat, file manager, and admin
  (~40 call sites). Browser "prevent additional dialogs" can no longer
  silently break flows like rename.

Coding-flow upgrades (harness over model — the coding-quality pass):

- **Orientation pack** (`runtime/context_pack.py`): a char-budgeted repo map
  (one line per source file — symbols + imports, cached on a tree
  fingerprint) plus the workspace's `JAYNET.md`/`AGENTS.md`/`CLAUDE.md`,
  prepended to `code.delegate` and architect plan/executor spawns
  (`tools.code.repomap` in runtime.yaml).
- **Verify baseline pre-run**: a verified run's check now runs once BEFORE
  the agent starts; a final failure identical to that pre-existing baseline
  passes as "not worse" (stated in the report), so pre-existing red is never
  chased or blamed on the change. Tamper and vacuous-pass guards unchanged.
- **Isolated delegation**: `code.delegate isolated:true` runs the coder in a
  throwaway git worktree (`.jaynet-worktrees/<id>-<suffix>`, own
  `jaynet/<id>-<suffix>` branch, per-call unique, hidden from the user's
  `git status` via `.git/info/exclude`) via a new spawn `work_root_path`
  kwarg confined to the parent's roots; the tool result carries commit
  count + diff stat + untracked files, only truly empty worktrees (no
  commits, no diff, nothing untracked, inspection clean) auto-clean, and
  merge/discard goes through the confirmation-gated git tools.
- **Per-unit architect verify**: UNITS now parse `- <step> | check: <cmd>`;
  when every unit has a check (`architect.per_unit_verify`, default on),
  each unit runs as its own executor spawn mechanically gated on its check,
  stopping at the first failure — prompt-level self-checking becomes
  harness-enforced.
- **Coding eval suite**: six new cases (`code-bugfix`, `code-refactor`,
  `code-feature-spec`, `code-spec-conflict-trap`, `code-weakened-test`,
  `code-orientation`) covering hidden-test discipline, behavior-preserving
  refactors, TDD order, the spec-vs-test trap, test-weakening honesty, and
  symbol navigation.

Harness todo list (ToDos side panel):

- New `todos` tool + per-run `TodoList` state (`runtime/todos.py`): the agent
  plans multi-step work as a structured list (set/update/add/remove/clear;
  pending/working/done/failed/skipped, at most one working) and the web UI
  renders it live in a collapsible right-edge panel — vertical "ToDos" toggle
  strip (label + done/total count stay visible when collapsed), per-item
  expander with description and the model's notes, collapsed by default. Every change emits a full-snapshot `todos` SSE event
  (reconnect- and replay-safe); the loop re-injects a compact rendering each
  turn so the list survives compaction (its own trailing system message when
  the working anchor is off, folded into the anchor when on). The architect
  flow's UNITS become the list automatically, and a spawned executor's
  updates forward to the parent's panel and state.

Behavioural eval harness (Admin → Eval):

- YAML test cases (`evals/` seeds + `$ORCH_DATA/custom/evals/`) run scripted
  or adaptive multi-turn conversations through the real agent loop — an
  unattended toolset (confirmation-gated tools excluded, except the
  sandbox-confined `fs.write`/`fs.edit` which run auto-approved against the
  per-case sandbox; cloud `llm.call` stays in but auto-denied, so privacy
  gates are really tested), which also redirects the memory/RAG stores, so a
  run can neither pollute real memory nor pull it into a judge transcript —
  graded by a state-aware judge model: it sees the run's available tools,
  the live system prompt, relevant tool descriptions, the bodies of the
  skills the agent loaded, and a config slice next to the transcript
  (`eval:` config section; cloud alias with local-specialist fallback,
  temperature 0). The only budget is $.
- Results, judge notes and pass-rate trends persist in `eval.db`, with a
  Statistics view (KPI cards, daily pass-rate/score trend, per-case
  flakiness, A/B period comparison, per-brain results); failures produce
  deduplicated WHAT/CAUSE/FIX proposals — nothing auto-applies. Accepting
  one applies to the custom layer only: prompt/skill tweaks extend the
  shipped artifact's overlay copy (a skill tweak is live on the next
  `skill.load` — no restart), tool descriptions are replaced via
  `custom/tool-overrides.yaml`, whitelisted config knobs go through the
  override path, bug-for-dev writes a ready-to-paste issue.
- Flags grow an "include private context" opt-in (default off) and a
  "make test" button that drafts a case from a flag's coroner report via a
  local model only — flagged content never leaves the box.
- `eval.run` / `eval.list` / `eval.report` tools let the agent self-test;
  cases share via `.jaypack`. 14 seed cases ship in `evals/`.
- Benchmark shootouts (Admin → Eval → Benchmark): run the same suite under N
  variants — a variant is a label + model alias + sampler overrides (e.g.
  `temperature: 0`, fixed `seed`) + reps — recorded under the label as the
  result's brain, with a per-variant comparison matrix (pass rate / avg
  score / cost / elapsed per case + overall). Pinned sampling applies to
  cross-model variants too (`sampling_force` run-override opt-in); variant
  aliases are validated at submit; a benchmark-wide cost ceiling
  (`eval.benchmark_max_cost_usd`, default $10) caps total spend.

Gate prompt overlay:

- The shipped `prompts/orchestrator-gate.md` stays pristine. Live edits —
  the Admin → Prompt tab and accepted eval prompt-tweaks — write an overlay
  in the data dir that wins while present, apply to the next run, and can be
  reverted to the shipped prompt, so deploys never conflict with live prompt
  edits.

Install simplification + pre-1.0 cleanup:

- `scripts/setup.sh` (full installer: prereqs, venvs, env file with
  auto-generated secrets, systemd units, linger) and `scripts/quickstart.sh`
  (one-command minimal install: prebuilt llama-server + model download)
- `scripts/orch --doctor` — install validator (10 checks with fix hints);
  `scripts/pull-model` — interactive HuggingFace GGUF downloader
  (`ORCH_MODELS`, default `$ORCH_HOME/models`)
- LiteLLM master key now optional for localhost-only installs (render omits
  it when `LITELLM_MASTER_KEY` is unset)
- runtime.yaml typo guard: boot warns on unknown config sections with
  "did you mean …" hints
- Preset hygiene: dead `.conf` keys removed (`PREDICT`, `MAIN_GPU`,
  `SYSTEM_PROMPT` — parsed nowhere), the four portable confs carry
  `HOST`/`PORT` so the documented `--preset` file-mode contract holds,
  `BACKEND` documented as display metadata, chat templates live in
  `$ORCH_MODELS/chat_templates/` (out of the repo), and the launcher's
  `.conf` parser expands `$ORCH_MODELS` textually; the eval cases table's
  Latest column fits 3-digit scores
- Default model set defined (docs/models.md): fresh installs seed
  brain = Qwen3-4B, embed/rerank = Qwen3 0.6B (all Apache-2.0) — code
  fallbacks, shipped presets and quickstart all point there; existing
  presets.db catalogs are untouched (seed applies to empty DBs only)
- Ports (`ORCH_LITELLM_PORT`, `ORCH_WEB_PORT`) and trusted proxy IP
  (`ORCH_FORWARDED_ALLOW_IPS`) configurable via the env file
- Retired `llama-brain1`/`llama-specialist` units (process manager owns
  models); templates moved to `example_configs/` with `.example` naming;
  version shown in the web UI; `docs/models.md` license-clean model picks

Pre-public security hardening (full third-party audit, read-only → fixes):

- **Missing sandbox now fails gated, not open**: when the firejail binary
  isn't on PATH, `code.run`/`code.execute` require human confirmation and
  the verifier refuses to run bare — previously they ran unsandboxed
  *ungated* on any host without firejail (every fresh non-Arch install)
- Browser tools (`web.render`, `browser.screenshot`, `browser.pdf`) now
  intercept every in-browser request and block loopback/link-local/metadata
  targets — closes the redirect-based SSRF bypass of the fetch guard;
  `pdf.create` renders fully offline (all network aborted except data: URIs)
- Web console: paste-jacking XSS in the composer's smart paste fixed
  (inert DOMParser); 2FA confirm/disable now throttled like login; request
  bodies capped (streaming 413s; restore ≤ `web.max_restore_mb`, studio
  import ≤ 5 MB, 4 MB global JSON cap); logout invalidates the session
  server-side; unknown-user login runs a dummy PBKDF2 (no timing oracle);
  new password hashes use 600k iterations (per-hash count, old ones keep
  verifying); admin-created accounts enforce the same ≥8-char minimum
- Agent runtime: a sub-agent spawn is refused when the parent's cost/token
  ceiling is already spent (previously the child ran *unlimited*);
  malformed model tool-calls degrade to an error result instead of an
  internal-error run abort; `trace.log_content: false` now strips every
  content-bearing event kind; gate-prompt overlay + tool-override writes
  are atomic; `job.start` env is scrubbed like `code.run`
- Tools: `git.pull`/`git.push` reject URL/`ext::` remotes like fetch;
  `web.request` drops Authorization/Cookie on cross-origin redirect hops;
  `.jaypack` import rejects decompression bombs (20 MB uncompressed cap)
- Shipped config neutralized: no live LAN IPs (SearXNG endpoint, trusted
  proxy default), no author paths (`$ORCH_MODELS` in presets, relative
  tools templates, binaries seed emptied — existing preset DBs keep their
  values, `$ORCH_LLAMA` expands in `env_setup`); `.gitignore` covers
  quickstart artifacts (bin/, *.bak, .env, *.part)
- Documented (accepted, docs/security.md): scheduled runs auto-approve
  gated tools by default; outbound GETs are an ungated exfiltration
  channel for a prompt-injected agent; managed child processes inherit
  the service env

## 0.9.0

First tagged release. Feature-complete daily driver; the 0.9.x line is
contract-hardening toward 1.0 — see
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
