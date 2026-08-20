"""Tests for the graphify plugin: runner status lifecycle, graph.* tools, and
the per-project routes (registered only when the plugin is loaded)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from runtime.plugins import PluginInfo
from tests.conftest import run

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "graphify"


import importlib.util  # noqa: E402


def _import(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = _import("gf_runner_test", PLUGIN_DIR / "runner.py")
graph_tools = _import("gf_tools_test", PLUGIN_DIR / "tools" / "graph.py")


# ---- runner status lifecycle -------------------------------------------------

def _mk_project(projects: Path, owner: str = "u", pid: str = "p1") -> Path:
    d = projects / owner / pid
    (d / "files").mkdir(parents=True)
    (d / "project.json").write_text('{"id": "p1", "name": "P"}')
    return d


def test_status_none_then_dirty(tmp_path):
    projects = tmp_path / "projects"
    _mk_project(projects)
    st = runner.read_status(projects, "u", "p1")
    assert st["state"] == "none" and st["building"] is False
    # no graph.json yet — dirty is a no-op
    runner.mark_dirty(projects, "u", "p1")
    assert runner.read_status(projects, "u", "p1")["state"] == "none"
    # a graph appears (built elsewhere) → ready, then file change → dirty
    root = runner.graph_root(projects, "u", "p1")
    root.mkdir(parents=True)
    (root / "graph.json").write_text('{"nodes": [1, 2], "edges": [1]}')
    assert runner.read_status(projects, "u", "p1")["state"] == "ready"
    runner.mark_dirty(projects, "u", "p1")
    st = runner.read_status(projects, "u", "p1")
    assert st["dirty"] is True


def test_graph_counts(tmp_path):
    g = tmp_path / "graph.json"
    g.write_text('{"nodes": [1, 2, 3], "edges": [1]}')
    assert runner._graph_counts(g) == (3, 1)
    g.write_text("garbage")
    assert runner._graph_counts(g) == (0, 0)


def test_start_build_refuses_missing_project(tmp_path):
    # The refusal path returns before any asyncio.create_task, so no event
    # loop is needed; the running-job path is covered by the web test below.
    ok, msg = runner.start_build(tmp_path, "u", "ghost", {})
    assert ok is False and "no such project" in msg


def test_cancel_without_job_is_false(tmp_path):
    assert runner.cancel_build("u", "nobody") is False


# ---- graph.* tools (no graphify CLI needed for the error paths) --------------

def _ctx(project_id, projects_dir):
    from runtime.tool_base import ToolContext
    return ToolContext(request_id="t",
                       config={"web": {"projects_dir": str(projects_dir)}},
                       budget=None, owner="u", project_id=project_id)


def test_tools_require_project(tmp_path):
    for tool in (graph_tools.GraphBuild(), graph_tools.GraphQuery(),
                 graph_tools.GraphExplain(), graph_tools.GraphPath(),
                 graph_tools.GraphStatus()):
        res = run(tool.execute({}, _ctx(None, tmp_path)))
        assert res.status == "error"
        assert "no project" in res.error


def test_query_requires_built_graph(tmp_path):
    projects = tmp_path / "projects"
    _mk_project(projects)
    res = run(graph_tools.GraphQuery().execute(
        {"question": "q"}, _ctx("p1", projects)))
    assert res.status == "error"
    assert "graph.build" in res.error


def test_status_tool_reports_none(tmp_path):
    projects = tmp_path / "projects"
    _mk_project(projects)
    res = run(graph_tools.GraphStatus().execute({}, _ctx("p1", projects)))
    assert res.status == "ok"
    assert res.result["state"] == "none"


def test_all_graph_tools_private():
    for tool in (graph_tools.GraphBuild(), graph_tools.GraphQuery(),
                 graph_tools.GraphExplain(), graph_tools.GraphPath(),
                 graph_tools.GraphStatus()):
        assert tool.private is True


# ---- plugin routes via the web harness ---------------------------------------

@pytest.fixture
def graph_app(web_app, monkeypatch):
    """web_app with the graphify plugin forced to 'loaded' (routes registered;
    the graphify CLI itself is absent, so builds end in state 'error')."""
    from runtime import plugins as plugin_loader
    info = PluginInfo(name="graphify", version="0.1.0", description="",
                      origin="builtin", dir=PLUGIN_DIR, enabled=True,
                      state="loaded", has_routes=True)
    monkeypatch.setattr(plugin_loader, "load", lambda cfg, reg: [info])
    return web_app()


@pytest.mark.asyncio
async def test_graph_status_and_build_lifecycle(graph_app, web_client):
    async with web_client(graph_app) as c:
        pid = (await c.post("/api/projects", json={"name": "G"})).json()["id"]
        r = await c.get(f"/api/projects/{pid}/graph/status")
        assert r.status_code == 200 and r.json()["state"] == "none"
        await c.put(f"/api/projects/{pid}/file?path=a.py", content=b"x=1\n")
        r = await c.post(f"/api/projects/{pid}/graph/build")
        assert r.status_code == 200
        # graphify isn't installed in the test venv → the job ends in error
        for _ in range(200):
            st = (await c.get(f"/api/projects/{pid}/graph/status")).json()
            if st["state"] == "error":
                break
            await asyncio.sleep(0.05)
        assert st["state"] == "error"
        assert st.get("error")


@pytest.mark.asyncio
async def test_graph_routes_owner_scoped(graph_app, web_client):
    async with web_client(graph_app) as c:
        pid = (await c.post("/api/projects", json={"name": "G"})).json()["id"]
    # a second user must not see the first user's graph endpoints
    graph_app.state.users.create("bob", "pw2", is_admin=False)
    async with web_client(graph_app, username="bob", password="pw2") as c2:
        r = await c2.get(f"/api/projects/{pid}/graph/status")
        assert r.status_code == 404
        r = await c2.get(f"/api/projects/{pid}/graph/viz")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_plugin_absent_means_no_routes(web_app, web_client):
    """Default config (graphify disabled) → the endpoints simply don't exist."""
    app = web_app()
    async with web_client(app) as c:
        pid = (await c.post("/api/projects", json={"name": "G"})).json()["id"]
        r = await c.get(f"/api/projects/{pid}/graph/status")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_admin_plugins_list_and_toggle(web_app, web_client, monkeypatch,
                                             tmp_path):
    # Point the loader's layers at this checkout (the test env's ORCH_HOME
    # may be the live install) and an empty installed dir.
    from runtime import paths
    monkeypatch.setattr(paths, "PLUGINS_BUILTIN_DIR",
                        Path(__file__).resolve().parent.parent / "plugins")
    monkeypatch.setattr(paths, "PLUGINS_DIR", tmp_path / "none")
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/plugins")
        assert r.status_code == 200
        plugins = {p["name"]: p for p in r.json()["plugins"]}
        assert "graphify" in plugins
        g = plugins["graphify"]
        assert g["origin"] == "builtin"
        assert g["enabled"] is False and g["state"] == "disabled"
        r = await c.post("/api/admin/plugins/graphify/toggle",
                         json={"enabled": True})
        assert r.status_code == 200
        assert "restart" in r.json()["note"]
        r = await c.get("/api/admin/plugins")
        g = {p["name"]: p for p in r.json()["plugins"]}["graphify"]
        assert g["enabled"] is True
        # unavailable, not loaded: either the checkout predates 1.1.0
        # (requires_jaynet gate) or the venv lacks the graphify package.
        assert g["state"] == "unavailable"
        assert g["missing"] or "requires" in g["reason"]
        r = await c.post("/api/admin/plugins/nope/toggle", json={"enabled": True})
        assert r.status_code == 404
