"""Admin plugin management routes (list + enable/disable + plugin UIs).

Plugins themselves are discovered/loaded by runtime/plugins.py at startup;
this module is only the admin surface. Toggling persists as a config override
(same mechanism as admin → Config) and applies to runtime.config live, but
tools/routes/hooks register at boot — the response says restart is required.

A plugin may ship a static admin UI (a `ui/` dir with index.html + assets);
it is served under /api/admin/plugins/<name>/ui/ — the /api/admin prefix
means the auth middleware gates it to admins, no per-route check. Plugin JS
calls the plugin's own routes; by convention plugin admin APIs live under
/api/admin/plugins/<name>/api/ so they get the same gate for free.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse


def register(app, s):
    runtime = s.runtime
    users = s.users

    @app.get("/api/admin/plugins")
    async def admin_plugins_list():
        from runtime import plugins as plugin_loader
        return {"plugins": [i.as_dict() for i in plugin_loader.scan(runtime.config)]}

    @app.post("/api/admin/plugins/{name}/toggle")
    async def admin_plugins_toggle(name: str, request: Request):
        body = await request.json()
        enabled = bool((body or {}).get("enabled"))
        from runtime import plugins as plugin_loader
        known = {i.name for i in plugin_loader.scan(runtime.config)}
        if name not in known:
            raise HTTPException(status_code=404, detail="no such plugin")
        dotpath = f"plugins.{name}.enabled"
        cur = users.get_config_overrides()
        cur[dotpath] = enabled
        users.set_config_overrides(cur)
        d = runtime.config.setdefault("plugins", {}).setdefault(name, {})
        d["enabled"] = enabled
        return {"ok": True, "plugin": name, "enabled": enabled,
                "note": "restart jaynet-web to apply — plugins load at startup"}

    def _ui_root(name: str) -> Path:
        from runtime import plugins as plugin_loader
        infos = {i.name: i for i in plugin_loader.scan(runtime.config)}
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
