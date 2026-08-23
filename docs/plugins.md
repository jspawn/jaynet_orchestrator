# Plugins

Plugins are **optional capability bundles** — installed by choice, toggleable,
and unable to break JayNet when disabled or broken. They extend JayNet through
a small explicit hook API, never through core internals.

The rule for what may be a plugin: if a run would be silently *wrong* without
it (budgets, taint/privacy, trace, confinement, todos, eval), it is **core**
and can never be a plugin. If a run is just less *capable* without it, it's a
plugin candidate.

## Using plugins

Admin → **Plugins** lists every discovered plugin with its state:

- **loaded** — active
- **disabled** — present but off (the default for repo-shipped builtins,
  which usually need extra pip packages)
- **unavailable** — enabled but unusable; the missing pip packages or the
  `requires_jaynet` mismatch is shown

Toggling persists (as a config override, same mechanism as admin → Config)
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

- **`.jayplugin` pack** — Admin → Plugins → **Install .jayplugin…**, then hit
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
# then admin → Plugins → enable graphify (applies live from then on)
```

Then, in any project: the files panel gets a **graph bar** (build / rebuild /
view / report), and the agent gains private `graph.*` tools —
`graph.build`, `graph.status`, `graph.query`, `graph.explain`, `graph.path` —
plus a hint in the project prompt prefix when a graph exists. The graph lives
at `<project>/graphify-out/` and is deleted with the project. File changes
mark it stale; with `auto_rebuild` on, a rebuild starts automatically after
a quiet window (default: off, rebuild via the files panel or `graph.build`).

Config (`plugins.graphify.*` in runtime.yaml / admin → Config):

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

### benchlab — public benchmark tasks as eval cases

Imports tasks from public agent benchmarks and converts them into eval cases
(Admin → Eval), so you can compare brains — or harness changes — on
standardized tasks instead of only home-grown ones. No pip dependencies;
containers only in full mode. Lite-mode grading runs the tasks' pytest
suites in the **service interpreter** — make sure `pytest` is installed in
the service venv (it's in `requirements-test.txt`; without it, imported
lite cases fail grading with a clear "No module named pytest").

Setup: admin → Plugins → enable benchlab (applies live — no restart; it has
no pip dependencies). Then either
press **open** on its row for the plugin's own admin page (fetch catalog,
import lite/full/GAIA, live job status), or drive it from chat:
`bench.fetch` (clones the Terminal-Bench catalog into
`$JAYNET_DATA/benchlab/`), `bench.import` (writes `tb-*`/`gaia-*` cases into
the custom evals layer), `bench.sources` (what's imported). The cases show up
in Admin → Eval and work with suite runs and the Benchmark compare tab like
any other case.

- **Terminal-Bench** ([laude-institute/terminal-bench](https://github.com/laude-institute/terminal-bench),
  Apache-2.0) in two modes. **Lite** (default): a curated container-free
  subset (~10 stdlib-only tasks), graded by their own embedded pytest
  suites, invisible to the agent. **Full** (`bench.import` with
  `mode: full`, needs rootless podman): any catalog task, built into a
  per-task container image (cached; builds need network), executed with
  `code.execute` running *inside* the container against the real task
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
  README.md       # optional: rendered in the Plugins tab
```

```yaml
name: myplugin
version: 0.1.0
description: What it adds, one line.
requires_jaynet: ">=1.1.0"     # only ">=" is evaluated
dependencies: [somepackage]    # pip import names, checked before loading —
                               # missing → "unavailable" (hard gate)
requires_bins: [podman]        # executables features degrade without —
                               # reported in the Plugins tab, never blocking
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
- **Routes** — `routes.py` with `register(app, s)`, same contract as
  `web/routes_*.py`; registered after core routes, so core always wins.
  Scope per-user data by `s._owner(request)` exactly like core routes do.
  Convention: **plugin admin APIs live under `/api/admin/plugins/<name>/api/`**
  — the auth middleware's `/api/admin` gate then applies automatically, no
  per-route checks.
- **Admin UI** — a `ui/` directory (index.html + assets, fully standalone,
  no CDN links) is served admin-gated at `/api/admin/plugins/<name>/ui/` and
  gets an **open** button in the Plugins tab. The page calls the plugin's
  own admin API; benchlab's `ui/index.html` + `routes.py` are the template,
  including background-job polling for long operations.
- **Packaging** — Admin → Plugins → **export** produces a `.jayplugin`
  (a `.jaypack` of kind `plugin`: the whole directory under
  `payload/<name>/`, `__pycache__` excluded). Installs via
  Admin → Plugins → **Install .jayplugin…** or
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
routes, skill) and `plugins/benchlab/` (tools, routes, admin UI — the
cleaner starting point). Tests: `tests/test_plugins.py`,
`tests/test_plugin_ui_routes.py`, `tests/test_graphify_plugin.py`.
