"""Admin plugin management routes (list + enable/disable).

Plugins themselves are discovered/loaded by runtime/plugins.py at startup;
this module is only the admin surface. Toggling persists as a config override
(same mechanism as admin → Config) and applies to runtime.config live, but
tools/routes/hooks register at boot — the response says restart is required.
"""

from __future__ import annotations

from fastapi import HTTPException, Request


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
