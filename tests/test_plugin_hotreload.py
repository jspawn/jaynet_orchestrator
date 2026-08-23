"""Plugin hot-reload end to end: toggling in Admin → Plugins applies live —
tools, routes and UI appear/disappear without a restart — and a plugin that
shows up after boot (fresh .jayplugin install) can be loaded via 'load now'."""

from __future__ import annotations

import pytest

from runtime import paths

_TOOL_SRC = '''
from runtime.tool_base import Tool, ToolContext, ToolResult

class NsThing(Tool):
    name = "ns.thing"
    description = "hot-reload test tool"
    parameters = {"type": "object", "properties": {}, "required": []}
    async def execute(self, args, ctx):
        return ToolResult(status="ok", result={})
'''

_ROUTE_SRC = '''
def register(app, state):
    @app.get("/api/admin/plugins/hotplug/api/ping")
    async def ping():
        return {"pong": True}
'''

# A plugin whose register() appends startup/shutdown hooks (the pattern core
# routes use, e.g. routes_procs). The hooks append marker lines to a file so
# the test can observe WHEN they ran.
_ROUTE_LIFECYCLE_SRC = '''
def register(app, state):
    @app.get("/api/admin/plugins/hotplug/api/ping")
    async def ping():
        return {"pong": True}
    async def _up():
        with open({marker!r}, "a") as f: f.write("up\\n")
    async def _down():
        with open({marker!r}, "a") as f: f.write("down\\n")
    state.startup_hooks.append(_up)
    state.shutdown_hooks.append(_down)
'''


def _write_plugin(root, name, *, routes=False, ui=False, lifecycle_marker=None):
    pdir = root / name
    (pdir / "tools" / "ns").mkdir(parents=True)
    (pdir / "plugin.yaml").write_text(f"name: {name}\nversion: '1.0'\n")
    (pdir / "tools" / "ns" / "thing.py").write_text(_TOOL_SRC)
    if routes:
        src = (_ROUTE_LIFECYCLE_SRC.replace("{marker!r}", repr(str(lifecycle_marker)))
               if lifecycle_marker else _ROUTE_SRC)
        (pdir / "routes.py").write_text(src)
    if ui:
        (pdir / "ui").mkdir()
        (pdir / "ui" / "index.html").write_text("hot ui")
    return pdir


@pytest.fixture
def hot_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLUGINS_DIR", tmp_path)
    return _write_plugin(tmp_path, "hotplug", routes=True, ui=True)


@pytest.mark.asyncio
async def test_plugin_hot_toggle_roundtrip(web_app, web_client, hot_plugin):
    app = web_app()
    reg = app.state.runtime.registry
    assert reg.get("ns.thing") is not None            # loaded at boot
    async with web_client(app) as c:
        assert (await c.get("/api/admin/plugins/hotplug/api/ping")).status_code == 200
        assert (await c.get("/api/admin/plugins/hotplug/ui/")).status_code == 200

        r = await c.post("/api/admin/plugins/hotplug/toggle",
                         json={"enabled": False})
        assert r.json()["note"] == "applied live — no restart needed"
        assert reg.get("ns.thing") is None
        assert (await c.get("/api/admin/plugins/hotplug/api/ping")).status_code == 404
        assert (await c.get("/api/admin/plugins/hotplug/ui/")).status_code == 404

        r = await c.post("/api/admin/plugins/hotplug/toggle",
                         json={"enabled": True})
        assert r.json()["note"] == "applied live — no restart needed"
        assert reg.get("ns.thing") is not None
        assert (await c.get("/api/admin/plugins/hotplug/api/ping")).status_code == 200
        assert (await c.get("/api/admin/plugins/hotplug/ui/")).status_code == 200


@pytest.mark.asyncio
async def test_plugin_load_now_after_late_install(web_app, web_client,
                                                  tmp_path, monkeypatch):
    """A plugin that appears in the installed dir AFTER boot (fresh pack
    install) lists as enabled-but-not-live and activates via the same
    toggle endpoint — the UI's 'load now' button."""
    plugins_dir = tmp_path / "installed"
    plugins_dir.mkdir()
    monkeypatch.setattr(paths, "PLUGINS_DIR", plugins_dir)
    app = web_app()
    _write_plugin(plugins_dir, "lateplug")            # lands after boot
    reg = app.state.runtime.registry
    assert reg.get("ns.thing") is None

    async with web_client(app) as c:
        r = await c.get("/api/admin/plugins")
        row = {p["name"]: p for p in r.json()["plugins"]}["lateplug"]
        assert row["state"] == "loaded" and row["live"] is False
        r = await c.post("/api/admin/plugins/lateplug/toggle",
                         json={"enabled": True})
        assert r.json()["note"] == "applied live — no restart needed"
        assert reg.get("ns.thing") is not None
        r = await c.get("/api/admin/plugins")
        row = {p["name"]: p for p in r.json()["plugins"]}["lateplug"]
        assert row["live"] is True


@pytest.mark.asyncio
async def test_plugin_lifecycle_hooks_run_on_hot_toggle(web_app, web_client,
                                                        tmp_path, monkeypatch):
    """A plugin's startup/shutdown hooks must fire on hot transitions too:
    startup on hot-enable (the lifespan already ran), shutdown on hot-disable
    — otherwise a third-party plugin copying the core pattern silently skips
    init/cleanup. Shutdown runs BEFORE unregister so cleanup can still use
    the plugin's own routes/tools."""
    marker = tmp_path / "lifecycle.log"
    monkeypatch.setattr(paths, "PLUGINS_DIR", tmp_path / "plugs")
    _write_plugin(tmp_path / "plugs", "hotplug", routes=True,
                  lifecycle_marker=marker)
    app = web_app()
    async with web_client(app) as c:
        # The test client doesn't run the ASGI lifespan, so the boot-time
        # startup hook hasn't fired — only hot transitions are observable
        # here (production runs them in the lifespan).
        assert not marker.exists()

        r = await c.post("/api/admin/plugins/hotplug/toggle",
                         json={"enabled": False})
        assert r.json()["note"] == "applied live — no restart needed"
        assert marker.read_text() == "down\n"          # cleanup ran on disable

        r = await c.post("/api/admin/plugins/hotplug/toggle",
                         json={"enabled": True})
        assert r.json()["note"] == "applied live — no restart needed"
        assert marker.read_text() == "down\nup\n"      # init ran on re-enable


@pytest.mark.asyncio
async def test_plugin_toggle_invalidates_openapi_schema(web_app, web_client,
                                                        hot_plugin):
    """FastAPI caches the OpenAPI schema at first render; a hot toggle must
    drop the cache or /docs shows routes that no longer exist (or misses
    ones that do)."""
    app = web_app()
    async with web_client(app) as c:
        assert (await c.get("/openapi.json")).status_code == 200
        assert app.openapi_schema is not None          # cached by the render
        await c.post("/api/admin/plugins/hotplug/toggle", json={"enabled": False})
        assert app.openapi_schema is None              # invalidated
        await c.post("/api/admin/plugins/hotplug/toggle", json={"enabled": True})
        assert app.openapi_schema is None
