"""Plugin UI serving (/api/admin/plugins/<name>/ui/), the benchlab plugin's
admin API, and .jayplugin install via the studio pack import endpoint."""

from __future__ import annotations

import io

import pytest

from runtime import jaypack, paths


@pytest.fixture
def ui_plugin(tmp_path, monkeypatch):
    """A minimal installed plugin with a ui/ dir, visible to scan()."""
    pdir = tmp_path / "uiplug"
    (pdir / "ui").mkdir(parents=True)
    (pdir / "plugin.yaml").write_text(
        "name: uiplug\nversion: '1.0'\ndescription: ui test\n")
    (pdir / "ui" / "index.html").write_text("<b>hello ui</b>")
    (pdir / "ui" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(paths, "PLUGINS_DIR", tmp_path)
    return pdir


@pytest.mark.asyncio
async def test_plugin_ui_served(web_app, web_client, ui_plugin):
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/plugins/uiplug/ui/")
        assert r.status_code == 200 and "hello ui" in r.text
        r = await c.get("/api/admin/plugins/uiplug/ui/app.js")
        assert r.status_code == 200 and "console.log" in r.text


@pytest.mark.asyncio
async def test_plugin_ui_unknown_and_traversal(web_app, web_client, ui_plugin):
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/plugins/nope/ui/")
        assert r.status_code == 404
        r = await c.get("/api/admin/plugins/uiplug/ui/%2e%2e/plugin.yaml")
        assert r.status_code in (400, 404)     # guard or router-level rejection
        r = await c.get("/api/admin/plugins/uiplug/ui/missing.txt")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_plugin_ui_scan_is_cached(web_app, web_client, ui_plugin,
                                        monkeypatch):
    """Iframe pages fetch many assets — the scan behind _ui_root is cached,
    so N asset requests cost one plugin scan, not N."""
    from runtime import plugins as plugin_loader
    calls = {"n": 0}
    orig = plugin_loader.scan

    def counting(config):
        calls["n"] += 1
        return orig(config)

    monkeypatch.setattr(plugin_loader, "scan", counting)
    app = web_app()
    async with web_client(app) as c:
        # Boot itself scans (plugin load); establish the baseline by warming
        # the route cache, then both asset hits must add no further scans.
        assert (await c.get("/api/admin/plugins")).status_code == 200
        base = calls["n"]
        assert (await c.get("/api/admin/plugins/uiplug/ui/")).status_code == 200
        assert (await c.get("/api/admin/plugins/uiplug/ui/app.js")).status_code == 200
        assert calls["n"] == base


@pytest.mark.asyncio
async def test_plugin_toggle_invalidates_scan_cache(web_app, web_client,
                                                    ui_plugin):
    """A toggle must not leave the cached scan serving a stale 'loaded'."""
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/plugins/uiplug/ui/")
        assert r.status_code == 200            # warms the cache
        r = await c.post("/api/admin/plugins/uiplug/toggle",
                         json={"enabled": False})
        assert r.status_code == 200
        r = await c.get("/api/admin/plugins/uiplug/ui/")
        assert r.status_code == 404            # disabled → not loaded → no UI


@pytest.mark.asyncio
async def test_benchlab_admin_api(web_app, web_client):
    """The benchlab plugin's routes registered on the app: overview works,
    bad import input 400s, a busy job 409s — without touching git/network."""
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "benchlab_plugin_routes_test",
        Path(__file__).resolve().parent.parent
        / "plugins" / "benchlab" / "routes.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    app = web_app()
    mod.register(app, None)
    async with web_client(app) as c:
        r = await c.get("/api/admin/plugins/benchlab/api/overview")
        assert r.status_code == 200
        body = r.json()
        assert {s["id"] for s in body["sources"]} == {"terminal-bench", "gaia"}
        r = await c.post("/api/admin/plugins/benchlab/api/import",
                         json={"source": "nope"})
        assert r.status_code == 400
        mod._JOB.update(state="running", op="test")
        r = await c.post("/api/admin/plugins/benchlab/api/fetch")
        assert r.status_code == 409
        mod._JOB.update(state="idle", op=None)


@pytest.mark.asyncio
async def test_plugin_pack_installs_via_studio_import(web_app, web_client,
                                                      tmp_path, monkeypatch):
    """End to end: build a plugin pack from a builtin-layer plugin, install it
    through the studio import endpoint — it lands in the installed layer and
    reports needs_restart."""
    builtin = tmp_path / "builtin"
    installed = tmp_path / "installed"
    (builtin / "packplug" / "tools").mkdir(parents=True)
    installed.mkdir()
    (builtin / "packplug" / "plugin.yaml").write_text(
        "name: packplug\nversion: '1.0'\ndescription: pack test\n")
    (builtin / "packplug" / "tools" / "x.py").write_text("# code\n")
    monkeypatch.setattr(paths, "PLUGINS_BUILTIN_DIR", builtin)
    monkeypatch.setattr(paths, "PLUGINS_DIR", installed)

    data = jaypack.build_pack("plugin", "packplug")
    assert jaypack.inspect_pack(data)["kind"] == "plugin"

    app = web_app()
    async with web_client(app) as c:
        r = await c.post("/api/admin/studio/import",
                         files={"file": ("packplug.jayplugin", io.BytesIO(data),
                                         "application/zip")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["kind"] == "plugin"
        assert body["needs_restart"] is True
        assert (installed / "packplug" / "plugin.yaml").is_file()
        # second install without overwrite → 409
        r = await c.post("/api/admin/studio/import",
                         files={"file": ("packplug.jayplugin", io.BytesIO(data),
                                         "application/zip")})
        assert r.status_code == 409
