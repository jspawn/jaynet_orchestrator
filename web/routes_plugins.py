"""Admin plugin management routes (list + enable/disable + plugin UIs).

Plugins themselves are discovered/loaded by runtime/plugins.py at startup;
this module is the admin surface. Toggling persists as a config override
(same mechanism as admin → Config), mutates runtime.config, AND applies live:
enable registers the plugin's tools/hooks/skills/routes in-process, disable
removes exactly what it added (runtime/plugins.py enable_live/disable_live).
A restart is only needed for newly installed pip dependencies.

A plugin may ship a static admin UI (a `ui/` dir with index.html + assets);
it is served under /api/admin/plugins/<name>/ui/ — the /api/admin prefix
means the auth middleware gates it to admins, no per-route check. Plugin JS
calls the plugin's own routes; by convention plugin admin APIs live under
/api/admin/plugins/<name>/api/ so they get the same gate for free.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

# scan() walks every plugin dir and re-reads capped READMEs — cheap at two
# plugins, linear afterwards. Plugin UIs are iframes whose every asset hit
# goes through the same scan, so cache the result briefly. Toggling
# invalidates; hot-reload covers apply, so the cache is the only lag.
_SCAN_TTL = 5.0


def register(app, s):
    runtime = s.runtime
    users = s.users
    _cache = {"t": 0.0, "infos": []}

    def _scan():
        now = time.monotonic()
        if now - _cache["t"] < _SCAN_TTL:
            return _cache["infos"]
        from runtime import plugins as plugin_loader
        infos = plugin_loader.scan(runtime.config)
        _cache.update(t=now, infos=infos)
        return infos

    @app.get("/api/admin/plugins")
    async def admin_plugins_list():
        handles = getattr(runtime, "plugin_handles", {})
        out = []
        for i in _scan():
            d = i.as_dict()
            # live = actually registered in-process right now. A plugin can be
            # enabled-but-not-live (fresh .jayplugin install, or unavailable
            # at boot) — the UI offers a "load now" button for those.
            d["live"] = i.name in handles
            out.append(d)
        return {"plugins": out}

    @app.post("/api/admin/plugins/{name}/toggle")
    async def admin_plugins_toggle(name: str, request: Request):
        from runtime import plugins as plugin_loader
        body = await request.json()
        enabled = bool((body or {}).get("enabled"))
        known = {i.name: i for i in _scan()}
        info = known.get(name)
        if info is None:
            raise HTTPException(status_code=404, detail="no such plugin")
        dotpath = f"plugins.{name}.enabled"
        cur = users.get_config_overrides()
        cur[dotpath] = enabled
        users.set_config_overrides(cur)
        d = runtime.config.setdefault("plugins", {}).setdefault(name, {})
        d["enabled"] = enabled
        _cache["t"] = 0.0   # enabled flags feed scan() — don't serve stale

        # Hot apply: the handle dict is the truth for what's live, scan() for
        # what's wanted. Re-scan AFTER the config flip — a previously
        # disabled plugin was never dep-checked, so only the fresh state
        # tells us whether enabling is actually possible. enable_live /
        # disable_live cover tools, hooks, skills, routes and the skills
        # catalog; the UI is scan-driven already.
        info = {i.name: i for i in _scan()}[name]
        handles = getattr(runtime, "plugin_handles", {})
        note = "applied live — no restart needed"
        if enabled:
            if info.state == "unavailable":
                note = (f"still unavailable ({info.reason}) — install the "
                        "missing pieces, then restart")
            elif name not in handles:
                handles[name] = plugin_loader.enable_live(
                    info, registry=runtime.registry, app=app, state=s,
                    runtime=runtime)
        else:
            handle = handles.pop(name, None)
            if handle is not None:
                plugin_loader.disable_live(
                    handle, registry=runtime.registry, app=app, state=s,
                    runtime=runtime)
            else:
                note = "was not loaded — nothing to undo"
        return {"ok": True, "plugin": name, "enabled": enabled, "note": note}

    def _ui_root(name: str) -> Path:
        infos = {i.name: i for i in _scan()}
        info = infos.get(name)
        if info is None or not info.has_ui or info.state != "loaded":
            raise HTTPException(status_code=404, detail="no such plugin UI")
        return (info.dir / "ui").resolve()

    @app.get("/api/admin/plugins/{name}/ui")
    async def admin_plugin_ui_index(name: str):
        return await admin_plugin_ui(name, "")

    @app.get("/api/admin/plugins/{name}/ui/{path:path}")
    async def admin_plugin_ui(name: str, path: str):
        root = _ui_root(name)
        dest = (root / (path or "index.html")).resolve()
        if dest != root and root not in dest.parents:   # traversal guard
            raise HTTPException(status_code=400, detail="bad path")
        if not dest.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(str(dest))
