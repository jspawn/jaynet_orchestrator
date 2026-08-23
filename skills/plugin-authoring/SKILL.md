---
name: plugin-authoring
description: >
  Build a JayNet plugin end to end: decide when a plugin (not a skill, chain
  or plain tool) is the right vehicle, scaffold manifest/tools/hooks/routes/
  admin-UI, honor the trust and privacy rules, test it, and package it as a
  shareable .jayplugin. Load when asked to create, extend, or package a
  plugin, or when a plugin won't load, shows "unavailable", or its UI 404s.
---

# Plugin Authoring

A plugin is JayNet's biggest extension vehicle: **tools + hooks + HTTP routes
+ an admin UI + skills**, bundled as one optional, toggleable directory that
an admin installs explicitly. Use it when a capability needs several of those
together, or carries optional dependencies. If you only need know-how, write
a skill; only a fixed pipeline, a chain; only one function, a custom tool
(Studio). Living references: `plugins/benchlab` (tools + routes + UI, clean
start) and `plugins/graphify` (adds hooks + skills, the full workout).

## 1. The layout

```
plugins/<name>/
  plugin.yaml     # manifest (required)
  tools/          # optional: tools/<ns>/<verb>.py — Tool subclasses
  hooks.py        # optional: functions named after runtime.hooks.HOOK_NAMES
  routes.py       # optional: register(app, s) — FastAPI endpoints
  ui/             # optional: static admin UI (index.html + assets, NO CDN)
  skills/         # optional: SKILL.md layer (origin plugin:<name>)
  README.md       # what it does + what to install — rendered in the Plugins tab
```

Two install layers: `$JAYNET_HOME/plugins` (builtin, ships with the repo,
default OFF) and `$JAYNET_DATA/plugins` (installed, default ON, survives git
pulls). Develop directly in the installed layer — then packaging is one
export. **Toggling applies live** (disable → re-enable reloads changed code,
no restart); only newly added pip dependencies still need a restart.

## 2. plugin.yaml

```yaml
name: my-plugin                  # letters, digits, dash, underscore
version: 0.1.0
description: One honest sentence — this is all the admin sees before enabling.
requires_jaynet: ">=1.2.0"       # only >= supported
dependencies: [some_pip_module]  # import names; missing → state "unavailable"
requires_bins: [git, podman]     # executables; missing is REPORTED, never blocking
```

- `dependencies` are a hard gate (import would fail) — keep the list exact.
- `requires_bins` is for features that degrade: the plugin loads anyway and
  the feature checks `shutil.which` at call time (see benchlab full mode).

## 3. tools/

One `Tool` subclass per file, discovered automatically:

```python
from runtime.tool_base import Tool, ToolContext, ToolResult

class Hello(Tool):
    name = "my.hello"            # namespace.verb — the namespace is the
                                 # keyword-selection unit, pick it deliberately
    read_only = True             # when it changes nothing
    private = True               # result may not leave the box (taints the run)
    requires_confirmation = True # when it mutates anything
    description = ("What it does, when to use it, when NOT to. This text is "
                   "the only steering the model gets — small brains act on "
                   "explicit hints, not vibes.")
    parameters = {"type": "object",
                  "properties": {"text": {"type": "string"}},
                  "required": ["text"]}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(status="ok", result={"echo": args["text"]})
```

Rules that keep the harness honest: declare `private` on anything that reads
local data; declare `requires_confirmation` on anything that writes; truncate
big results; return `status="error"` with an actionable message (say which
tool or step comes next, like web.fetch's render hints).

## 4. hooks.py (optional)

Define functions named exactly after `runtime.hooks.HOOK_NAMES` (e.g.
`augment_project_context`, `project_tools`, `on_project_file_changed`,
`on_project_delete`). They fire synchronously on the caller's thread — mark
state, never do work. A throwing hook is logged and skipped, but a slow one
slows every run.

## 5. routes.py (optional)

```python
from fastapi import HTTPException, Request

def register(app, s):
    @app.get("/api/admin/plugins/my-plugin/api/status")
    async def status():
        return {"ok": True}
```

- **Admin APIs live under `/api/admin/plugins/<name>/api/`** — the auth
  middleware's `/api/admin` prefix gate then applies automatically. Any other
  path is reachable by every logged-in user: scope user data with
  `s._owner(request)` (see graphify's routes).
- Never import `web/*` and never mutate core state. The contract surface is
  `register(app, s)` + `runtime.hooks` + `runtime.paths` (+ your own modules,
  loaded by file path — see `_load_importer` in benchlab's tools/bench.py).

## 6. ui/ (optional)

`ui/index.html` is served, admin-gated, at `/api/admin/plugins/<name>/ui/`
and gets an **open** button in the Plugins tab. It must be fully standalone:
inline CSS/JS, **no CDN links** (JayNet is local-first and may be offline).
From the page, call your own admin API:

```js
const API = "/api/admin/plugins/my-plugin/api";
const d = await (await fetch(API + "/status")).json();
```

Long operations: run them as one background `asyncio` task and poll a job
endpoint (benchlab's routes.py + ui/index.html are the template, including
"resume the status view on revisit").

## 7. README.md — the install contract

Rendered in the Plugins tab. Answer, in order: what does it do; what must be
installed (`pip install …`, system binaries, tokens in env); how to verify it
works; its honest limits. If setup needs anything not expressible in
`dependencies`/`requires_bins`, it goes here.

## 8. Test it

- Unit tests live in the repo's `tests/` for builtin-bound plugins: construct
  tools with a `ToolContext` (see `tests/conftest.py` fixtures), stub network
  and subprocesses, and run
  `ORCH_HOME=/srv/orch-dev .venv/bin/python -m pytest tests/ -q` plus
  `.venv/bin/ruff check plugins`.
- Smoke the wiring by hand: enable (applies live) → the tool appears in
  Admin → Tools; the UI opens from the Plugins tab; a chat run can call the
  tool by name.

## 9. Package as .jayplugin

A plugin pack is a `.jaypack` zip with kind `plugin` (the whole directory
under `payload/<name>/`). Export it from Admin → Plugins → **export**
(or `runtime.jaypack.build_pack("plugin", "<name>")` on the CLI). Import on
another JayNet via Admin → Plugins → **Install .jayplugin…** → **load now**
on its row (no restart). The
guards are automatic: manifest check, 5 MB cap, zip-slip rejection, no
clobber without overwrite. The pack carries executable Python — say so when
you share it; the trust model is "only install code you audited".

## 10. Pre-ship checklist

- description fields (manifest + every tool) tell the truth, incl. costs and
  what NOT to use it for
- `private` / `requires_confirmation` flags set where real
- no `web/*` imports, no core-state mutation, hooks are fast
- works with missing optional bins (graceful feature degrade + clear error)
- README lists every pip/bin/token the plugin needs
- tests green, ruff clean, pack installs on a throwaway data dir
