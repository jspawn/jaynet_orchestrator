# Plugins

Plugins are **optional capability bundles** — installed by choice, toggleable,
and unable to break JayNet when disabled or broken. They extend JayNet through
a small explicit hook API, never through core internals.

The rule for what may be a plugin: if a run would be silently *wrong* without
it (budgets, taint/privacy, trace, confinement, todos, eval), it is **core**
and can never be a plugin. If a run is just less *capable* without it, it's a
plugin candidate.

## Using plugins

Admin → **Harness → Plugins** lists every discovered plugin with its state:

- **loaded** — active
- **disabled** — present but off (the default for repo-shipped builtins,
  which usually need extra pip packages)
- **unavailable** — enabled but unusable; the missing pip packages or the
  `requires_jaynet` mismatch is shown

Toggling persists (as a config override, same mechanism as admin → Harness → Runtime)
and applies **live** — tools, hooks, skills, routes and admin UIs appear or
disappear without a restart (new runs only; in-flight runs keep their frozen
toolset). A restart is only needed when a plugin gains **new pip
dependencies** — those install into the venv, which no hot path can do.

Two layers, same split as skills:

| Layer | Location | Default |
|---|---|---|
| builtin | `$JAYNET_HOME/plugins/` (ships with the repo) | disabled |
| installed | `$JAYNET_DATA/plugins/` (survives git pulls) | enabled |

An installed plugin with the same name overrides the builtin one.

**Installing** a plugin, two ways:

- **`.jayplugin` pack** — Admin → Harness → Plugins → **Install .jayplugin…**, then hit
  **load now** on its row (no restart).
  Packs are how plugins are shared (export button on every row); they carry
  the whole plugin directory with the same guards as `.jaypack` (5 MB cap,
  zip-slip rejection, no clobber without overwrite, inner `plugin.yaml`
  validated at upload).
- **Manual** — copy or clone the directory into `$JAYNET_DATA/plugins/<name>/`,
  install its declared pip dependencies into the runtime venv, restart once
  (for the deps), enable.

Either way: check the plugin's row afterwards — it shows declared pip
dependencies, a **needs bin:** note for executables some features use
(`requires_bins` — reported, never blocking), and its **readme** (what to
install, what it does). A plugin with an admin UI gets an **open** button
(served admin-gated at `/api/admin/plugins/<name>/ui/`).

**Trust model:** plugins are Python running in JayNet's process with full
trust — there is no sandbox. A `.jayplugin` is executable code, exactly like
cloning a repo and running it. Only install code you audited, and only as
admin.

## Shipped plugins

### graphify — per-project graphs

Wraps the [graphify](https://github.com/Graphify-Labs/graphify) CLI
(Apache-2.0). Maps a project's files into a queryable graph: code
via local tree-sitter AST (no LLM, nothing leaves the box), docs/PDFs via a
semantic pass against the configured LiteLLM alias.

Setup:

```bash
uv pip install --python .venv/bin/python graphifyy
# restart once so the running process picks up the new package,
# then admin → Harness → Plugins → enable graphify (applies live from then on)
```

Then, in any project: the files panel gets a **graph bar** (build / rebuild /
view / report), and the agent gains private `graph.*` tools —
`graph.build`, `graph.status`, `graph.query`, `graph.explain`, `graph.path` —
plus a hint in the project prompt prefix when a graph exists. The graph lives
at `<project>/graphify-out/` and is deleted with the project. File changes
mark it stale; with `auto_rebuild` on, a rebuild starts automatically after
a quiet window (default: off, rebuild via the files panel or `graph.build`).

Config (`plugins.graphify.*` in runtime.yaml / admin → Harness → Runtime):

- `model` — LiteLLM alias for the semantic pass (default `local-specialist`).
  Point it at a cloud alias only if the project's docs may leave the box.
- `token_budget` / `max_concurrency` — semantic-pass chunking, tuned small
  for local models.
- `max_output_tokens` — per-call output cap (default 8192). **The speed
  lever:** the extractor generates up to this cap, so on a dense 27B a full
  cap is ~8 min per doc chunk. Lower it, or point `model` at a faster alias
  (e.g. the MoE brain) for large doc piles.
- `label_communities` — let the LLM name graph communities in the report
  (off by default; costs tokens).
- `auto_rebuild` / `auto_rebuild_delay_s` — debounced auto-rebuild on file
  change (default off / 120 s). Only projects that already HAVE a graph are
  rebuilt (a first build is always a deliberate click — the semantic pass
  is the most expensive thing the plugin does). Errors are not retried
  until the next change.
- `wiki_nodes` — deterministic wiki extractor (default on, free — no LLM):
  the project wiki (`files/wiki/`) becomes one node per page plus
  `references` edges for `[text](page.md)` and `[[Page Name]]` links,
  appended before clustering so wiki pages get communities and appear in
  the report/viz. `graph.seed_kg` carries them into the kg as type `wiki`.

Knowledge-surface bridges (both project-scoped, both surfaces `private`):

- **`graph.seed_kg`** — feeds the project graph into the curated knowledge
  graph: nodes become kg entities named `'<project>/<node>'` (kind as type,
  file/community in attrs, `origin: graphify`), edges become relations with
  their confidence. Asks for confirmation (bulk write), merges on re-seed.
  That's the cross-project "where else do we do X" answer via `kg.query`.
- **`rag.search` excerpt** — a project-bound `rag.search` automatically gets
  a `graph_excerpt` attached: the 1-hop project-graph neighborhood around
  its hits (via the `rag_excerpt` hook; absent plugin → plain chunk hits).

### benchlab — public benchmark tasks as eval cases

Imports tasks from public agent benchmarks and converts them into eval cases
(Admin → Studio & Eval → Eval), so you can compare brains — or harness changes — on
standardized tasks instead of only home-grown ones. No pip dependencies;
containers only in full mode. Lite-mode grading runs the tasks' pytest
suites in the **service interpreter** — make sure `pytest` is installed in
the service venv (it's in `requirements-test.txt`; without it, imported
lite cases fail grading with a clear "No module named pytest").

Setup: admin → Harness → Plugins → enable benchlab (applies live — no restart; it has
no pip dependencies). Then either
press **open** on its row for the plugin's own admin page (fetch catalog,
import lite/full/GAIA, live job status), or drive it from chat:
`bench.fetch` (clones the Terminal-Bench catalog into
`$JAYNET_DATA/benchlab/`), `bench.import` (writes `tb-*`/`gaia-*` cases into
the custom evals layer), `bench.sources` (what's imported). The cases show up
in Admin → Studio & Eval → Eval and work with suite runs and the Benchmark subtab like
any other case.

- **Terminal-Bench** ([laude-institute/terminal-bench](https://github.com/laude-institute/terminal-bench),
  Apache-2.0) in two modes. **Lite** (default): a curated container-free
  subset (~10 stdlib-only tasks), graded by their own embedded pytest
  suites, invisible to the agent. **Full** (`bench.import` with
  `mode: full`, needs rootless podman): any catalog task, built into a
  per-task container image (cached; builds need network), executed with
  `code.run`/`code.execute` running *inside* the container against the real task
  environment, graded by the task's own tests run in-container. Builds
  execute the upstream Dockerfiles' `RUN` lines at build time — rootless
  podman, user namespaces, but you are running third-party build scripts;
  that's the trusted-content step, like `git clone && make` anywhere else.
- **GAIA** Level-1 ([gaia-benchmark/GAIA](https://huggingface.co/datasets/gaia-benchmark/GAIA),
  CC-BY-4.0, gated): exact-match QA graded by `expect.answer_exact_any`
  (GAIA-scorer normalization). Needs your own `HF_TOKEN` in the env file —
  the dataset is gated and the token is never logged.

Honesty note: lite mode and GAIA are *JayNet-condition* runs — no containers,
our sandbox, our tool surface. Full TB mode runs the real per-task
environments in containers, close to the official protocol; the remaining
divergences are no agent-phase network, our tool surface instead of a raw
shell, and our per-case budgets instead of their step limits. Numbers
compare your brains and harness variants against each other and over time;
treat cross-leaderboard comparisons as approximate.

### h5i — the red-team browser

[h5i](https://github.com/h5i-dev/h5i) (pure Rust — ~3× faster, ~86% less
memory than Chromium) is a browser whose engine is the HTTP client: the page
the agent drives and the traffic it produces are one auditable session.
`browser.browse` covers open (with traffic `capture`)/snapshot/click/type/
submit/scroll/waitfor/extract/`structured` (JSON-LD/OpenGraph/meta — portals
publish listings there)/transcript/markdown/screenshot/requests/audit with
domain allowlists. Since h5i 0.4 it doubles as the **authorized
security-testing lane**: the optional recon/websec plugins (separate
binaries, `h5i plugin install --from <path>`) add the endpoint ledger and
replay/mutate/diff/findings on the captured traffic — scope discipline
applies (authorized targets only; no complete PoC, no vulnerability).
Chromium (Playwright) stays for PDFs. No pip dependencies; the h5i binary is
the only requirement.

### jev — decision-model routing (Open-Jev / jevify)

Wires a decision model into JayNet: typed questions (choice / yes-no /
score) answered with probabilities — one forward pass, no generated text.
No pip dependencies in JayNet; the model server is a sidecar you run
yourself (setup in the plugin's README, shown in admin → Harness → Plugins). Two
local sidecar options, both speaking the same Jev API:

- **[jevify](https://github.com/fidecastro/jevify) (recommended)** — no new
  weights at all: it makes a model you already serve (e.g. the coding
  specialist) answer typed questions from its next-token logprobs and
  serves the Jev API on its own port. Recipe template ships as
  `plugins/jev/jevify-recipe.example.yaml`: probe, serve, point
  `plugins.jev.base_url` at it.
- **[Open-Jev](https://github.com/Zefan-Cai/Open-Jev)** — the trained
  checkpoint (LoRA + decision head on Qwen3.5-2B/9B).

- **`jev.decide`** — the brain can ask choice / yes-no / score questions and
  get probabilities instead of guessing JSON.
- **Delegation routing** — when enabled, each incoming request is classified
  into a strength tag from your `models.strengths` registry; a confident
  pick routes the run (same delegate/swap-in note as the keyword router),
  anything else falls back to keywords. Configure via `plugins.jev.*`
  (`route`, `route_threshold`, timeouts); see the plugin README. **Tested as
  a delegation classifier for the "models won't delegate" problem
  (2026-09-22): the idea works** — hosted TypeSafe Jev routed 20 real
  prompts nearly perfectly where keywords miss — **but the open 2B
  checkpoint doesn't** (OOD training data), and cloud-routing every request
  is the wrong privacy default, so it ships disabled. Numbers and revisit
  conditions: docs/brain-bakeoff.md lesson 5.

### clm — contrastive decision model (CLM-8B)

The jev successor candidate, same System One wire contract:
[CLM](https://github.com/Contrastive-LM/CLM) is a purpose-built contrastive
decision model (state + candidates in, calibrated probabilities out — no
generation, ~30 ms warm on CPU). Stdlib-only client; the sidecar pair
(encoder preset `presets/embed-qwen3-8b-clm.conf` + `clm-serve` venv) is
documented in the plugin README.

- **`clm.decide`** — choice / yes-no / score questions with probabilities.
- **`clm.rank`** — best-of-N ordering (solutions, tool names, next moves).
- **Routing hook** — opt-in like jev's. The 2026-09-27 route bench
  (docs/clm-bakeoff.md) measured 18.9% top-1 on 243 real requests vs
  keyword 13.2% and hosted Jev 73.3% — not routing-trained enough, so the
  hook ships off; the tools stay useful on their own.

### imagegen — local text-to-image (stable-diffusion.cpp)

`image.generate` renders images on your own GPU — no cloud, nothing leaves
the box. One `sd-server` binary plus three model files (DiT GGUF, text
encoder, VAE — Qwen-Image-2.1 tested); setup in the plugin README. VRAM is
handled by design: a generation hibernates the configured slot (default
specialist), serves the diffusion backend on loopback, stages the PNG as a
user download, and a keep-warm reaper (default 600 s) restores the slot so
image batches pay the swap once. sd-server is registered on the JayNet
shutdown path — no GPU-resident orphan if the service stops mid keep-warm.

### omnivoice — local text-to-speech (omnivoice.cpp)

`audio.speak` turns text into a WAV on your own GPU — 600+ languages,
voice design via `instructions` ("warm elderly female, calm"), zero-shot
voice cloning from a reference WAV (`audio.clone` registers it server-side
by name). One `tts-server` binary plus two small GGUFs (~1 GB, Q8_0 —
[OmniVoice](https://huggingface.co/k2-fsa/OmniVoice) via
[omnivoice.cpp](https://github.com/ServeurpersoCom/omnivoice.cpp)); setup
in the plugin README. The server is OpenAI-compatible on loopback, starts
on first call, and a keep-warm reaper (default 600 s) shuts it down when
idle — no slot hibernation needed at this footprint. Cloned voices live in
server RAM: re-register after a backend restart. Registered on the JayNet
shutdown path like imagegen.

## Writing a plugin

The guided version of this section lives in the `plugin-authoring` skill —
load it (`skill.load("plugin-authoring")`) and the agent will walk the whole
build: scaffold, manifest, tools, hooks, routes, UI, tests, packaging.

A plugin is a directory with a `plugin.yaml` manifest:

```
plugins/graphify/
  plugin.yaml     # name, version, description, requires_jaynet,
                  # dependencies[], requires_bins[]
  tools/          # optional: <ns>/<verb>.py Tool subclasses
  skills/         # optional: <name>/SKILL.md skill layer
  hooks.py        # optional: functions named after runtime.hooks.HOOK_NAMES
  routes.py       # optional: register(app, state) — web route contract
  ui/             # optional: static admin UI (index.html; standalone, no CDN)
  README.md       # optional: rendered in the Plugins subtab
```

```yaml
name: myplugin
version: 0.1.0
description: What it adds, one line.
requires_jaynet: ">=1.1.0"     # only ">=" is evaluated
dependencies: [somepackage]    # pip import names, checked before loading —
                               # missing → "unavailable" (hard gate)
requires_bins: [podman]        # executables features degrade without —
                               # reported in the Plugins subtab, never blocking
```

- **Tools** — loaded like `$JAYNET_DATA/custom` tools: concrete
  `runtime.tool_base.Tool` subclasses, skipped (logged) on error, name
  collisions with existing tools refused. Declare `private = True` whenever
  output derives from user/project data.
- **Skills** — merged into the skill catalog as `origin: "plugin:<name>"`
  (precedence: builtin < plugin < custom).
- **Hooks** — `hooks.py` may define any of `runtime.hooks.HOOK_NAMES`:
  - `augment_project_context(owner, pid, meta, files_root) -> str | None` —
    text appended to the `[Project: …]` prompt prefix (keep it to a line or two)
  - `project_tools(owner, pid, meta, files_root) -> list[str] | None` — tool
    names force-added to the run's frozen auto-selected toolset. The keyword
    selector only sees the message text, so tools the prefix hint advertises
    (see `augment_project_context`) must be declared here or the model sees
    the hint but can't call the tools. Unknown and admin-disabled names are
    dropped; not fired when the caller pinned an explicit tool list.
  - `on_project_delete(owner, pid)` — cleanup after a project was deleted
  - `on_project_file_changed(owner, pid, path, projects_dir)` — fired on web
    file write/delete/rename AND on the agent's own `fs.write`/`fs.edit`
    inside a project-bound run (cheap marking only, never heavy work);
    `projects_dir` is the resolved root, honoring a custom `web.projects_dir`
  - `on_run_end(payload)` — fired once per run at finish on every terminal
    path (ok/error/cancelled/budget), with `payload = {"request_id",
    "status", "tools": [used tool names], "config": <runtime.yaml dict>}`.
    For per-run cleanup that must not depend on the model remembering (the
    h5i plugin closes the run's browser session here, resolving the binary
    from `plugins.h5i.binary` first). The fire runs through
    `asyncio.to_thread`, so a blocking subprocess is allowed — still keep it
    short and best-effort.
- **Routes** — `routes.py` with `register(app, s)`, same contract as
  `web/routes_*.py`; registered after core routes, so core always wins.
  Scope per-user data by `s._owner(request)` exactly like core routes do.
  Convention: **plugin admin APIs live under `/api/admin/plugins/<name>/api/`**
  — the auth middleware's `/api/admin` gate then applies automatically, no
  per-route checks. If `register()` appends to `s.startup_hooks` /
  `s.shutdown_hooks` (async callables, the core pattern), they run in the
  lifespan at boot/shutdown AND on hot toggles: startup fires on hot-enable,
  shutdown fires on hot-disable — before anything is unregistered, so
  cleanup can still use the plugin's own routes and tools.
- **Admin UI** — a `ui/` directory (index.html + assets, fully standalone,
  no CDN links) is served admin-gated at `/api/admin/plugins/<name>/ui/` and
  gets an **open** button in the Plugins subtab. The page calls the plugin's
  own admin API; benchlab's `ui/index.html` + `routes.py` are the template,
  including background-job polling for long operations.
- **Packaging** — Admin → Harness → Plugins → **export** produces a `.jayplugin`
  (a `.jaypack` of kind `plugin`: the whole directory under
  `payload/<name>/`, `__pycache__` excluded). Installs via
  Admin → Harness → Plugins → **Install .jayplugin…** or
  `runtime.jaypack.install_pack`; then **load now** on its row (no restart).

Plugin modules are imported **by file path**, not as a package — import
sibling files relative to `__file__` (see `plugins/graphify/tools/graph.py`
for the pattern). One warning that bit us in production: every
`spec_from_file_location` + `exec_module` creates a **fresh module with fresh
module-level state** — if tools/hooks/routes each exec a shared helper file,
you get three independent copies of its globals (the v1.1.0 graphify plugin
split its build-job registry exactly this way). Any shared file must be
loaded once and cached in `sys.modules` under one fixed name, as
`_load_runner()` does. Plugin config lives under `plugins.<name>.*` in
runtime.yaml and reaches tools via `ctx.config["plugins"]["<name>"]`.

Keep hooks fast (they fire on the request path), keep state under the
project dir or `$JAYNET_DATA`, and never import `web/*` from tools or hooks.

Reference implementations: `plugins/graphify/` (manifest, tools, hooks,
routes, skill), `plugins/benchlab/` (tools, routes, admin UI — the
cleaner starting point) and `plugins/h5i/` (a single policy-gated tool —
the minimal skeleton). Tests: `tests/test_plugins.py`,
`tests/test_plugin_ui_routes.py`, `tests/test_graphify_plugin.py`.
