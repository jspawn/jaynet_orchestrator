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


def _write_plugin(root, name, *, routes=False, ui=False):
    pdir = root / name
    (pdir / "tools" / "ns").mkdir(parents=True)
    (pdir / "plugin.yaml").write_text(f"name: {name}\nversion: '1.0'\n")
    (pdir / "tools" / "ns" / "thing.py").write_text(_TOOL_SRC)
    if routes:
        (pdir / "routes.py").write_text(_ROUTE_SRC)
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
