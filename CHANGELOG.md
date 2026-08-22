# Changelog

Breaking changes and release notes. Versions are git tags; the stable API
contract lives in `docs/api.md`, upgrade procedure in `docs/upgrading.md`.

## Unreleased

## 1.1.6 — 2026-08-22

**Patch.** Docs-only: the README's references table now credits
Graphify-Labs/graphify (the Apache-2.0 engine behind the graphify plugin's
`graph.*` tools) alongside the j-space suite. No code changes.

**Upgrade:** Pull, restart, done — or skip it; nothing runtime-relevant.

## 1.1.5 — 2026-08-21

**Patch.** The j-space skill ships: deliberate-workspace doctrine as an
on-demand skill, a `run.badge` tool so skills can show their active mode
live in chat, two new eval cases guarding the doctrine, and a
complexity-gate nudge toward it. No breaking changes.

- **New skill: j-space** — an adapted vendoring of the Apache-2.0 J-Space
  Cognition Suite V3.6 (prompt doctrine, NOT the interpretability research
  it borrows vocabulary from): the brain classifies a task fast/full/loop,
  loads only the module the task earns, and keeps a `.jspace/WORKSPACE.md`
  ledger of settled/open/next for long work. Its plan stays in the harness
  todo list (the upstream design is explicit that the ledger is *not* a
  task list), pinned via `context.pin` for compaction survival. Modules
  and references ship verbatim; `LICENSE`/`THIRD_PARTY_NOTICES.md`/
  `NOTICE` ride along.
- **New core tool: `run.badge`** — a short live status label on a run
  (footer line + debug view, replayed with saved chats). Skills use it to
  show which mode is active; j-space badges `j-space: full` / `j-space:
  loop` at the gate and on every pass change. Registered in the core
  toolset incl. the trivial-message minimal set, so a skill loaded
  mid-run can always badge.
- **Evals:** two new cases — `j-space-loop` (multi-file rename driven
  through the loop pass: plan-before-edit, badge, tests actually run) and
  `j-space-floor` (a "quick, no ceremony" request that isn't fast must be
  escalated, not answered from the request alone).
- **Complexity gate nudges toward j-space at 3+.** Deliberately a nudge,
  not an auto-load: the skill's gate only works when the model classifies
  the task itself, and its own doctrine forbids loading machinery the task
  didn't earn. Default-on was rejected for the same reason — it would tax
  the fast path and dilute the gate prompt.
- **Audit closure (2026-08-21):** root `THIRD_PARTY_NOTICES.md` lists the
  j-space vendoring (Apache-2.0 — "all are MIT" was no longer accurate),
  release notes for v1.1.3/v1.1.4 backfilled, and the eval graph prebuild
  subprocess env now goes through `scrub_env`, same posture as the MCP
  stdio bridge.

**Upgrade:** Pull, restart, done. j-space costs nothing until loaded —
say "use the j-space skill" on a hard task, or let a 3+ complexity rating
nudge it.


## 1.1.4 — 2026-08-21

**Patch.** Two real boot fixes found by the first live plugin eval run,
the eval harness learning to mirror web project context, and one-click
consolidation of eval prompt tweaks. No breaking changes.

- **Fix: plugin toggles never took effect.** Admin-persisted config
  overrides were applied *after* the runtime loaded plugins from the
  YAML-only config, so enabling a plugin in Admin → Plugins + restart
  registered no tools/hooks/routes while the Plugins tab reported
  "loaded" (live-confirmed with graphify). Overrides now merge before
  plugin discovery, with the users DB located via `load_config` so
  relative paths (`users_db: users.db`) anchor at the data dir exactly
  like the runtime's own resolution. As a side effect, `web.*`
  overrides (e.g. `web.cookie_secure`) actually reach the web config
  now.
- **Prompt tab: one-click consolidation of eval tweak bullets.**
  Accepted prompt-tweak proposals collect as dated bullets under an
  `<!-- eval-proposals -->` marker (capped at 5, then manual merge was
  required). New **Consolidate eval tweaks** button drafts a merged
  prompt with the eval judge model (bullets folded into the prose,
  marker dropped), shows it in the source editor for review, and
  **Apply consolidation** writes a timestamped backup next to the
  overlay before saving. Deliberately NO prompt-per-model versioning:
  the gate prompt is harness doctrine, not model tuning — the eval
  suite itself is the regression guard when the brain changes.
- **Eval: project-fixture cases get the web's project context.** Turn 1
  of a `project:` case now carries the same prefix the web layer
  prepends on project-bound runs — `[Project:]` banner, file tree, and
  plugin hints via the `augment_project_context` hook (graphify's
  "[Project graph] … prefer graph.query"). Without it the agent had
  graph tools but zero nudge: the first live `graph-orientation` run
  answered correctly via `fs.read` and judge-failed the rubric
  (score 3, "undiscoverable"). The `graph-orientation` rubric was also
  sharpened to grade the runtime-vs-source-edit distinction explicitly —
  the case is green on live (10/10, graph-only navigation).

Upgrade: pull, restart, done. If you enabled a plugin in Admin →
Plugins before this release and wondered why nothing happened: this
fixes it — the toggle takes effect with the restart.

## 1.1.3 — 2026-08-21

**Patch.** Whole-project review follow-ups: plugin tools in the catalog,
unambiguous graph naming, a second shipped chain, and project-bound eval
cases. No breaking changes.

- **Catalog covers plugins.** `scripts/gen_catalog.py` now scans
  `plugins/*/tools`, so `graph.*` appears in `docs/catalog.md` tagged with
  its plugin — previously plugin tools were in no reference table.
- **Naming: project graph vs knowledge graph.** graphify's map is now
  called "project graph" everywhere (tool descriptions, the project-prefix
  hint, skill, file-manager UI, docs); `kg.*` keeps "knowledge graph".
  The glossary disambiguates: derived/per-project vs curated/cross-chat.
  Tool names unchanged — no config impact.
- **Second shipped chain:** `knowledge-brief` — recalls from
  memory/kg/RAG first, fills gaps from the web, and marks each bullet
  `[known]` vs `[new]`.
- **Project-bound eval cases.** Eval cases gain `requires_tools` (skip
  cleanly when an install lacks the tools, e.g. plugin disabled) and
  `project` fixtures (files seeded into the per-case sandbox; optional
  graphify graph pre-built via the CLI). New case `graph-orientation`
  guards the "query the graph before grepping" doctrine. Internally this
  adds a server-side-only `run_overrides.config_patch` seam in the loop.

Upgrade: pull, restart, done.

## 1.1.2 — 2026-08-20

**Patch.** The v1.1.1 audit follow-ups (MCP manager robustness at the
YAML↔UI boundary) plus the admin tab reorder. No breaking changes.

- **MCP manager polish.** YAML-defined server names that violate the UI's
  slug rules are flagged at load time instead of blocking every save with a
  surprise 400; the manager shows when the list comes from runtime.yaml
  (first save takes over via config override; deleting *all* servers falls
  back to the YAML definitions — now warned about). Validation type-checks
  url/command/args/timeout_s for non-UI API clients; the Test button honors
  the per-server timeout and always probes fresh; the "mcp package not
  installed" hint no longer vanishes after a save.
- **Admin tabs reordered:** Status, Processes, Presets, Prompt, Config,
  Tools, MCP, RAG, Studio, Plugins, Eval, Flags, Users, Backup — MCP moves
  out of the Tools tab into its own group right after Tools; docs/admin.md
  sections follow the same order.
- **Docs:** admin.md documents the MCP servers section (incl. the args/env
  round-trip limits); configuration.md lists the mcp tool family.

Upgrade: pull, restart, done.

## 1.1.1 — 2026-08-20

**Hardening + MCP server manager.** The 1.1.0 plugin drop gets its audit
follow-ups (two real bugs fixed), and MCP servers move out of raw-YAML-only
editing into a proper admin UI.

- **Plugin fixes (post-1.1.0 audit).** The graphify plugin's build runner was
  imported three times under different module names — three independent job
  registries, so the duplicate-build guard failed across entry points and
  cancel-on-project-delete was dead. All entry points now share one cached
  module (regression-tested). Staleness marking ignored a custom
  `web.projects_dir` — the `on_project_file_changed` hook now receives the
  resolved root (signature gained a 4th parameter; plugin authors see
  docs/plugins.md). Plus: the loader survives malformed `plugins:` config
  instead of crashing boot, `status.json` writes are atomic, security.md
  documents the plugin trust surface.
- **Admin → Tools → MCP servers.** MCP servers were YAML-only and invisible
  in the admin UI (an empty `servers: {}` flattens to nothing in the Config
  editor). Now: list/add/edit/delete (stdio command+args+env or HTTP url,
  confirm-per-call toggle, timeout), a Test button that lists the server's
  tools, and a hint when the optional `mcp` package is missing. Saves apply
  live, no restart.
- **New plugin hook: `project_tools`.** A plugin can declare which tools a
  project-bound run must keep reachable; they are force-added to the frozen
  auto-selected toolset (unknown and admin-disabled names dropped, explicit
  caller tool lists stay authoritative). The graphify plugin uses it to keep
  `graph.*` callable whenever its project hint is injected — the keyword
  selector has no "graph" trigger, so before this the hint could advertise
  tools the model couldn't call.
- **New doc: `docs/playbook.md`** — the tool/skill/chain/plugin landscape in
  prose: what every piece does and is good at, how the pieces harmonize and
  where they compete, ending in a verdict. Written against the
  implementations, not just the descriptions; linked from the README.

Upgrade: pull, restart, done. The `on_project_file_changed` hook signature
changed — only relevant if you wrote a 1.1.0 plugin against it.

## 1.1.0 — 2026-08-20

**Plugin system + per-project knowledge graphs.** JayNet gains an
optional-capability layer: plugins are installable, toggleable bundles that
extend JayNet through a small hook API — disabled or broken plugins are never
imported, so they can't take the core down. The first shipped plugin maps any
project into a queryable knowledge graph.

- **Plugin system.** Two layers (repo `plugins/` builtins, default off;
  `<data>/plugins/` installed, default on), manifest-driven
  (`plugin.yaml` with `requires_jaynet` + pip dependency gates), admin
  Plugins tab with enable/disable (restart to apply). Plugins can contribute
  tools, skills, hooks (`augment_project_context`, `on_project_delete`,
  `on_project_file_changed`) and routes — see [docs/plugins.md](docs/plugins.md).
- **Graphify plugin (builtin, off by default).** Wraps the
  [graphify](https://github.com/Graphify-Labs/graphify) CLI: each project's
  files become a knowledge graph — code via local tree-sitter AST (no LLM),
  docs/PDFs via a semantic pass through your local LiteLLM alias. The agent
  gets private `graph.build/query/explain/path/status` tools and a
  query-before-grep hint in the project prompt; the files panel gets a graph
  bar (build / view / report). The graph lives at
  `<project>/graphify-out/` and is deleted with the project. Enable:
  `uv pip install --python .venv/bin/python graphifyy`, Admin → Plugins → enable, restart.
- `ToolContext.project_id` is now threaded through runs (incl. sub-agents)
  so project-scoped plugin tools resolve their storage correctly.

Upgrade: pull, restart, done. Nothing changes until you enable a plugin.

## 1.0.3 — 2026-08-20

**Hotfix.** One real bug on top of 1.0.2, plus doc-count corrections.

- **Admin → Tools no longer 500s on an undescribed tool.** The new
  per-tool descriptions used `splitlines()[0]` — a custom (Studio) tool
  with an empty description turned that into an IndexError and took the
  whole grid down. Now yields `""`, with a regression test.
- Changelog/release notes: corrected the 1.0.1 audit accounting (9 of 16
  suggestions in code, two more documented as accepted risks) and
  resynced the README version badge.

Upgrade: pull, restart, done.

## 1.0.2 — 2026-08-19

**Self-documenting admin + selftest fix round.** Found by running the
shipped selftest skill against the live install (kimi-k3 as brain) and by a
documentation pass over the admin tabs. Suite 1176 passed, ruff clean.

- **Admin → Config explains itself.** Every setting (~300 keys) shows a
  one-line explanation under its label, served from the new shipped
  `config/config-help.yaml`. A coverage test fails the suite when a key
  ships without help — the on-screen docs can't rot. New
  `docs/configuration.md` maps the config layers (YAML seed → DB overrides
  → per-user/per-run) and walks the editor sections.
- **Admin → Tools shows real descriptions.** The grid's tooltip was always
  empty — the API never sent a description field. Each of the 113 tools
  now carries its one-liner (the text the model reads) inline, and the
  filter matches it.
- **Headless-browser setup is distro-aware.** `browser.*`/`web.render`/
  `pdf.create` failed at RuntimeError on fresh installs (live selftest
  finding): `setup.sh --with-tools` now resolves the platform (existing
  system chromium / pacman / apt / `playwright install`) and
  `orch --doctor` reports which path wins with an install hint.
- **Cloud catalog fixes.** The `gemini-pro` seed pointed at a non-existent
  `gemini-3.5-pro`; the cloud store now rejects an OpenRouter `api_base`
  whose provider model lacks the `openrouter/` prefix — LiteLLM silently
  dropped such deployments and the alias vanished from `/imp`.
- **Eval proposals land cleaner.** Judge meta-phrasing ("Add a
  directive: …") is stripped when a prompt/skill tweak is accepted, so the
  live prompt overlay reads as directives to the model.
- **`code.patch`** — the lenient retry now passes `--recount` (live
  selftest finding).

Upgrade: pull, restart, done — no config or data migration.

## 1.0.1 — 2026-08-19

**Post-release audit round-trip.** The v1.0.0 full bug & security audit,
fixed end to end:
all four A-items, 14 of 17 B-nits, 9 of 16 suggestions in code — two more
are documented as accepted risks in `docs/security.md`, the rest deferred
as product decisions. Suite 1163 passed, ruff clean.

- **Cloud/privacy gates closed everywhere.** `verify.*` accepted a
  model-chosen cloud alias and sent graded content off-box with no approval
  and no taint refusal (the bug class the S1 audit closed for
  council/eval). Slash-command spawns (`/<tool> … model=<cloud>`) skipped
  the cloud spawn gate. Both now gate exactly like `llm.call`.
- **Gate consistency.** `git.fetch` (network egress to the configured
  remote) is confirmation-gated like pull/push; `trace.query` and
  `trace.mine` `all_owners=true` (cross-user trace read) now require
  confirmation.
- **Secrets hygiene.** Serving launches (llama-server) get a
  secret-scrubbed environment instead of the full orchestrator env, and
  `scrub_env` also drops `_PASSPHRASE`/`_PAT`/`_DSN` and `DATABASE_URL`-style
  DSNs. `users.db`/`chats.db`/data dir are chmod 0600/0700 in app code (the
  quickstart path has no systemd `UMask=0077` to rely on); `session.secret`
  is created `O_EXCL` 0600.
- **Supply chain.** quickstart pins llama.cpp (`b10343`) and sha256-verifies
  the download against GitHub's published asset digest (`--latest` opts back
  into floating). Runtime/web/tool deps ship pinned `requirements.lock`
  files, installed by setup.sh/quickstart.sh/CI (loose `.txt` = fallback).
- **Install correctness.** setup.sh, quickstart.sh and the setup doc require
  Python 3.11 (the code needs it; 3.10 used to pass the checks and die at
  import). A cleartext non-loopback bind prints a loud boot warning.
- **Robustness.** Prompt scheduler task can't be GC'd; ProcessManager
  spawn-failures count toward `max_restarts` instead of retrying forever;
  `budget-defaults.json` and `server.json` write atomically; one malformed
  `schedules.json` entry no longer stalls every scheduled prompt; blocking
  HF-metadata and binary-`--help` calls moved off the event loop; the login
  throttle's maps are bounded against unique-username sprays.
- **UI.** Escaping consistency pass in admin.html/app.js (the JSON-array
  config input was a real markup-breakage bug; process cards render via
  textContent/handler closures instead of inline `onclick`).
- **Ops.** Restore mirrors the backup whitelist (stray archive entries like
  `session.secret` are no longer swapped in); `.gitignore` covers `*.env`;
  setup.sh comments out unused provider `<key>` lines; both systemd units
  gain `NoNewPrivileges`/`PrivateTmp`/`ProtectSystem=full`.

## 1.0.0 — 2026-08-19

**Public-release milestone.** JayNet started as a personal learning project
and a nightly-driver experiment; 1.0.0 marks the point where the install is
documented for strangers (quickstart throwaway test, guided setup.sh, manual
path), the API contract is frozen (`docs/api.md`), the suite runs green in
CI on every push, and the codebase is MIT-licensed for everyone to use.

Changes since 0.9.8:

- **Mobile scrolling fixed.** Follow-to-bottom no longer traps touch users
  during a run — a downward finger drag releases it (previously only
  wheel/keys/scrollbar could), reaching the bottom re-engages.
- Housekeeping: the parked-work file is swept to the three real open items
  (GitHub Releases, managed vLLM, Android app); README polish.

## 0.9.8 — 2026-08-16

- **Nerd-mode prompt line, final form.** The ❯ glyph hangs in the log
  gutter (easy to spot, shell-style), the gold shine is back, the 118ch
  measure cap is gone (full-width terminal), and wrapped prompt lines sit
  flush with the first line instead of indenting.
- **Faster boot.** The specialist's boot stagger drops 45s → 20s
  (specialist2/3 keep the 5s ladder at 25/30).

## 0.9.7 — 2026-08-15

- **Structured preset editor.** The raw `.conf` textbox is now a form — one
  field per launch flag `start-model.sh` understands, typed (numbers,
  enums), with defaults and one-line help. `model file`, `mmproj` and
  `tools template` get **browse…** pickers confined to the models dir.
  The raw text stays behind an **advanced (raw .conf)** toggle; switching
  views is lossless (comments and unknown keys survive).
- **Model files browser.** Admin → Presets → **Browse model files…** opens
  the models dir as a collapsible folder tree (`.gguf`/`.jinja` by default,
  **show all** reveals the rest). Files a preset references are marked
  **★ preset-name**, and **Make preset from selected** drafts a new preset
  (name, model path, VRAM estimate) for the picked GGUF.
- **Per-binary flag help.** Each llama-server binary (Admin → Processes)
  has a **help** button showing its `--help` output; the preset form's
  `extra args` row links to the same viewer for the selected binary.
- **Service restart buttons.** Admin → Status can restart `litellm-proxy`
  and the web console itself (delayed + detached self-restart).
- **Fixes:** `start-model.sh` prefers the install venv's python (PyYAML on
  minimal distros/CI) · mobile ⋯ menu font · todo-panel collapse
  specificity · nerd-mode user-line wrap/shine polish · 2026-08-15 audit:
  keyed-endpoint probe shadowing, `$JAYNET_MODELS` conf expansion parity,
  binary-help cache invalidation, self-restart fallback logging.

## 0.9.6 — 2026-08-14

- **API keys for adopted (remote) endpoints.** A remote preset gains an
  **api key env** field (admin → Presets): the NAME of an env var in
  `~/.config/jaynet.env` holding the server's key. The key never enters the
  preset DB or litellm.yaml (rendered as `os.environ/…`); probes send it as
  a Bearer header, and a 401/403 now distinguishes "no key configured" from
  "key rejected". (2026-08-11 audit A3)
- **CI + lint baseline.** `.github/workflows/ci.yml` runs ruff and the full
  pytest suite on every push/PR; `ruff.toml` pins the rule set
  (E4/E7/E9/F/I/UP) after a one-time cleanup pass. Python minimum is now
  3.11 (web/server.py already used 3.11 syntax).
- **Scheduled, version-tagged eval runs.** Admin → Eval → Scheduled runs
  fires a suite unattended on an interval (selector `case:<id>` or
  `tag:<tag>`, 1–720 h) through the normal suite path — skipped while any
  suite runs, auto-disabled when its selector goes stale. Every eval result
  now records the JayNet **version** alongside the brain label, so eval.db
  is a longitudinal quality ledger across releases and brain swaps.
- **Near-duplicate loop guard.** The exact-args loop guard now also catches
  the classic overthinking pattern — the same search reworded ("price 2026
  CHF" → "24h price CHF 2026"). For query-like tools (`loop_guard.
  near_dup_tools`, default web.search/web.fetch/arxiv.search), two calls
  whose argument tokens overlap ≥ `near_dup_threshold` (0.75) count as
  duplicates: the third similar call is blocked with a synthesize-now error
  and feeds the wrap-up escalation. Genuinely different queries pass
  untouched — deep research is unaffected.
- **Benchmark-informed routing.** The Benchmark compare view crowns the
  leading variant (★ winner bar, mean pass rate tie-broken by score) and
  offers a one-click **route it**: assign the winning preset to a slot
  through the existing preset-slots API — human-gated, restart-to-apply,
  closing the shoot-out-then-swap loop.
- **Audit fixes (2026-08-14).** The `live_slot` and /imp dead-slot probes
  now forward a remote preset's API key (a keyed adopted endpoint no longer
  shows dead there); schedule-toggle PUT without `enabled` 400s instead of
  silently disabling; eval version lists sort numerically; CI also tests
  the declared Python 3.11 floor.

## 0.9.5 — 2026-08-13

- **Eval suites and benchmarks can be cancelled** (Admin → Eval): a Cancel
  button / `POST /api/admin/evals/cancel` stops the run after the case in
  flight finishes — later cases are skipped and the summary is marked
  cancelled.
- **Benchmark reps no longer wobble the statistics.** Eval results recorded
  under a benchmark variant are flagged, and the Statistics view (KPIs,
  trend, flakiness, per-case drilldown) counts live runs only by default; a
  new brain dropdown scopes every statistic to one variant label. Results
  recorded before this change stay in the default view.
- **Design refresh across the console.** Tool-call state moved into status
  dots with a gold running band; nerd mode gets a readable 118ch measure
  and a shared glyph gutter; the ctx meter is a fill bar; the admin coral
  was demoted to an accent stripe + ADMIN pill, with status pills carrying
  state dots; account/admin share the app's tokens, micro-label headers
  and tabular numerals. The composer keeps its classic transparent-gold
  icon layout (a circular-rail experiment was tried and reverted), and the
  nerd/chat-bubbles switch is a labeled toggle in the desktop header —
  mobile always follows your stored default.
- **FastAPI startup/shutdown hooks migrated to the lifespan API** — no
  behavior change, deprecation warnings gone.
- **`CONTEXT.md`** at the repo root: a code-facing glossary for AI-assisted
  dev sessions (term → module map), complementing `docs/glossary.md`.
- **`LEARNING_GUIDE.md` corrected and extended** — verified against the
  current code (tool count, eval flow, preset/slot model) and the best of
  the earlier cut material restored.

## 0.9.4 — 2026-08-12

- **`JAYNET_LLAMA` indirection removed.** It existed only to locate a GPU
  env script (`$JAYNET_LLAMA/rdna4-env.sh`) and was a silent no-op when unset.
  `tools.serve.env_setup` now ships empty — set an absolute path in Admin →
  Config if you have such a script. Existing configs that reference
  `$JAYNET_LLAMA` keep working (`$VARS` still expand; the job runner now
  expands them too, like the serve launcher always did).
- **CLI self-bootstraps: `scripts/orch` works as documented.** Run with a
  bare system python it re-execs into the checkout's `.venv` (click/rich live
  there), and it now loads `~/.config/jaynet.env` like the systemd units do —
  previously a CLI run outside the default `/srv/orchestrator` path resolved
  every path wrong (`orch --doctor` reported phantom failures on a healthy
  install).
- **setup.sh survives delete-and-reinstall.** It now stops existing
  `litellm-proxy`/`jaynet-web` units up front and clears `start-limit-hit`
  before enabling — previously a reinstall into a deleted tree left
  `Restart=always` crash-looping the units (203/EXEC) until systemd gave up,
  and the first healthy start needed a manual `reset-failed`.
- **Preset seed is now generic teaching examples.** The wolf-specific
  production presets (Fable/Tess/ornith/agents1/dolphin, Genesis brains,
  8B embedder) are replaced by two commented example presets —
  `brain-moe` (Qwen3-30B-A3B, MoE: ~3B active params = fast all-day brain)
  and `specialist` (Qwen2.5-Coder-32B, dense: stronger per token for code
  delegation) — both without model files, with per-knob explanations in
  `presets/*.conf`. Existing installs are untouched (their presets.db
  already holds the old seed). docs/models.md explains the MoE/dense pair.
- **Self-contained llama.cpp install trees now just run.** `start-model.sh`
  prepends `<bin>/../lib` to `LD_LIBRARY_PATH`, so a cmake-install layout
  (shared libs next to `bin/`) works without `ldconfig` or system-wide
  install.
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
