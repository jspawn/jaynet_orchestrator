# Handoff: writing a JayNet plugin

Paste this into a fresh AI session when you want to build a new plugin
(an optional, toggleable capability bundle). Read [docs/plugins.md](../docs/plugins.md)
for the user-facing side; this file is the builder's map.

## What a plugin is

A directory with a `plugin.yaml` manifest, living in `$JAYNET_HOME/plugins/`
(repo builtins, ship disabled) or `$JAYNET_DATA/plugins/` (installed, ship
enabled; same name overrides the builtin). **Disabled = never imported**, so
plugins can never take JayNet down. The reference implementation is
`plugins/graphify/` — copy its shape.

```
myplugin/
  plugin.yaml     # name, version, description, requires_jaynet: ">=1.1.0", dependencies[]
  tools/          # <ns>/<verb>.py — runtime.tool_base.Tool subclasses
  skills/         # <name>/SKILL.md — merged into the skill catalog
  hooks.py        # functions named after runtime.hooks.HOOK_NAMES
  routes.py       # register(app, s) — FastAPI routes, registered AFTER core
```

## The rules that matter

- **Plugin modules are imported by file path, not as a package.** Import
  sibling files relative to `__file__` — copy the `_load_runner()` pattern
  from `plugins/graphify/tools/graph.py`.
- **Core touchpoints are only:** `runtime.hooks` (register nothing yourself —
  `runtime/plugins.py` does it from your hooks.py), the Tool contract, the
  `register(app, s)` route contract, and `ctx.config["plugins"]["<name>"]`
  for your config. Never import `web/*` from tools/hooks.
- **Tools:** set `private = True` if output derives from user/project data
  (keeps it out of cloud models via the taint system). `ToolResult` needs
  `status` + `result` (pass `result=None` on errors). Read
  `tools/kg/graph.py` for the canonical style.
- **Hooks must be fast and never raise through** — they're wrapped in
  try/except, but a slow hook slows every request. Mark state, don't work.
- **Routes:** scope user data with `s._owner(request)` (None → `"_token"`),
  validate path params with `os.path.basename`, reuse `s.projects_dir` /
  `s.runtime.config` off the state namespace. Core routes win over yours.
- **Manifest `dependencies`** are import names checked via
  `importlib.util.find_spec` before loading; missing → admin sees
  "unavailable: missing dependencies …". Nothing is auto-installed.
- **Versioning:** `requires_jaynet` supports only `>=` comparisons.

## Enable + test loop

1. Drop the dir into `$JAYNET_DATA/plugins/myplugin/` (installed layer →
   enabled by default; builtin layer needs `plugins.myplugin.enabled: true`
   in runtime.yaml or admin → Plugins).
2. Restart `jaynet-web` (plugins load at startup; hot-reload is a non-goal).
3. Verify: admin → Plugins shows it "loaded"; tools appear in the tool
   catalog; skills appear with origin `plugin:myplugin`.

## Tests to add

- Loader behavior goes in `tests/test_plugins.py` style (tmp manifest dirs,
  monkeypatched `paths.PLUGINS_*` layers).
- Routes/tools: see `tests/test_graphify_plugin.py` — it forces the plugin
  "loaded" by monkeypatching `runtime.plugins.load` to return a `PluginInfo`
  with `state="loaded"`, then drives the endpoints through the `web_app` /
  `web_client` fixtures (no real external deps).
- Run: `.venv/bin/python -m pytest tests/ -q` and
  `.venv/bin/python -m ruff check runtime web tools scripts tests plugins`.
