"""Admin Schedules REST (web/routes_schedules.py): CRUD + validation, run
history, per-owner projects list, and the per-user schedules_allowed flag.
Endpoint tests drive FastAPI in-process (see docs/testing-harness.md)."""
import time

import pytest

from runtime.scheduler import ScheduleStore
from web import projects as PJ
from web.store import ScheduleRunStore


def _point_store(app, tmp_path):
    """Isolate the schedule JSON store per test (read at call time)."""
    path = str(tmp_path / "schedules.json")
    app.state.runtime.config.setdefault("tools", {})["schedule"] = {"store": path}
    return ScheduleStore(path)


def _runs(app):
    return ScheduleRunStore(app.state.runtime.config["web"]["chats_db"])


# ---- the auth store flag itself ----------------------------------------------
def test_schedules_allowed_flag(tmp_path):
    import web.auth
    us = web.auth.UserStore(str(tmp_path / "users.db"))
    us.create("alice", "password1")
    assert us.get_schedules_allowed("alice") is False
    assert us.get_schedules_allowed("ghost") is False
    assert us.set_schedules_allowed("alice", True) is True
    assert us.get_schedules_allowed("alice") is True
    assert us.set_schedules_allowed("ghost", True) is False
    row = [u for u in us.list() if u["username"] == "alice"][0]
    assert row["schedules_allowed"] is True


# ---- CRUD ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_list_delete(tmp_path, web_app, web_client):
    app = web_app()
    app.state.users.create("alice", "alicepass")
    store = _point_store(app, tmp_path)
    async with web_client(app) as c:
        r = await c.post("/api/admin/schedules", json={
            "owner": "alice", "prompt": "morning report", "kind": "once",
            "when": "+30m"})
        assert r.status_code == 200
        entry = r.json()["schedule"]
        assert entry["kind"] == "once" and entry["last_status"] is None
        assert entry["project_name"] is None

        r = await c.get("/api/admin/schedules")
        assert [s["id"] for s in r.json()["schedules"]] == [entry["id"]]

        r = await c.put(f"/api/admin/schedules/{entry['id']}",
                        json={"enabled": False})
        assert r.status_code == 200
        assert store.list()[0]["enabled"] is False
        assert (await c.put("/api/admin/schedules/nope",
                            json={"enabled": True})).status_code == 404

        assert (await c.delete(f"/api/admin/schedules/{entry['id']}")).status_code == 200
        assert store.list() == []
        assert (await c.delete(f"/api/admin/schedules/{entry['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_create_cron_computes_next_fire(tmp_path, web_app, web_client):
    app = web_app()
    app.state.users.create("alice", "alicepass")
    _point_store(app, tmp_path)
    async with web_client(app) as c:
        r = await c.post("/api/admin/schedules", json={
            "owner": "alice", "prompt": "standup", "kind": "cron",
            "cron": "0 9 * * 1-5"})
        assert r.status_code == 200
        s = r.json()["schedule"]
        assert s["kind"] == "cron" and s["cron"] == "0 9 * * 1-5"
        assert s["next_fire"] > time.time()


@pytest.mark.asyncio
async def test_create_validation_errors(tmp_path, web_app, web_client):
    app = web_app()
    app.state.users.create("alice", "alicepass")
    _point_store(app, tmp_path)
    async with web_client(app) as c:
        bad = [
            {"owner": "ghost", "prompt": "x", "kind": "once", "when": "+1h"},
            {"owner": "alice", "prompt": "  ", "kind": "once", "when": "+1h"},
            {"owner": "alice", "prompt": "x", "kind": "hourly"},
            {"owner": "alice", "prompt": "x", "kind": "once", "when": "someday"},
            {"owner": "alice", "prompt": "x", "kind": "every", "every": "often"},
            {"owner": "alice", "prompt": "x", "kind": "cron", "cron": "bogus"},
            {"owner": "alice", "prompt": "x", "kind": "cron",
             "cron": "0 0 30 2 *"},
            {"owner": "alice", "prompt": "x", "kind": "once", "when": "+1h",
             "project_id": "nope-123"},
        ]
        for body in bad:
            r = await c.post("/api/admin/schedules", json=body)
            assert r.status_code == 400, (body, r.status_code, r.text)
            assert r.json()["detail"]


# ---- projects + history --------------------------------------------------------
@pytest.mark.asyncio
async def test_projects_list_and_create_with_project(tmp_path, web_app,
                                                     web_client):
    app = web_app()
    app.state.users.create("alice", "alicepass")
    store = _point_store(app, tmp_path)
    meta = PJ.create_project(tmp_path / "projects", "alice", "Demo")
    async with web_client(app) as c:
        r = await c.get("/api/admin/schedules/projects", params={"owner": "alice"})
        assert r.status_code == 200
        assert r.json()["projects"] == [{"id": meta["id"], "name": "Demo"}]
        assert (await c.get("/api/admin/schedules/projects",
                            params={"owner": "bob"})).json()["projects"] == []

        r = await c.post("/api/admin/schedules", json={
            "owner": "alice", "prompt": "tidy up", "kind": "every",
            "every": "1d", "project_id": meta["id"]})
        assert r.status_code == 200
        assert r.json()["schedule"]["project_id"] == meta["id"]
        assert r.json()["schedule"]["project_name"] == "Demo"
        assert store.list()[0]["every_s"] == 86400


@pytest.mark.asyncio
async def test_history_endpoint(tmp_path, web_app, web_client):
    app = web_app()
    app.state.users.create("alice", "alicepass")
    store = _point_store(app, tmp_path)
    e = store.add({"owner": "alice", "prompt": "x", "kind": "once",
                   "next_fire": time.time() + 3600})
    runs = _runs(app)
    runs.add(e["id"], "alice", "r1", "ok", 2.0, "did it", 10)
    async with web_client(app) as c:
        r = await c.get(f"/api/admin/schedules/{e['id']}/history")
        assert r.status_code == 200
        rows = r.json()["runs"]
        assert len(rows) == 1 and rows[0]["run_id"] == "r1"
        assert rows[0]["status"] == "ok" and rows[0]["excerpt"] == "did it"
        r = await c.get("/api/admin/schedules/nope/history")
        assert r.status_code == 404


# ---- per-user flag endpoint ------------------------------------------------------
@pytest.mark.asyncio
async def test_user_schedules_flag_endpoint(tmp_path, web_app, web_client):
    app = web_app()
    app.state.users.create("alice", "alicepass")
    async with web_client(app) as c:
        r = await c.post("/api/admin/users/alice/schedules",
                         json={"allowed": True})
        assert r.status_code == 200
        assert app.state.users.get_schedules_allowed("alice") is True
        r = await c.post("/api/admin/users/alice/schedules",
                         json={"allowed": False})
        assert r.status_code == 200
        assert app.state.users.get_schedules_allowed("alice") is False
        r = await c.post("/api/admin/users/ghost/schedules",
                         json={"allowed": True})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_non_admin_is_forbidden(tmp_path, web_app, web_client):
    app = web_app()
    app.state.users.create("alice", "alicepass")
    _point_store(app, tmp_path)
    async with web_client(app, username="alice", password="alicepass") as c:
        assert (await c.get("/api/admin/schedules")).status_code == 403
        r = await c.post("/api/admin/schedules", json={
            "owner": "alice", "prompt": "x", "kind": "once", "when": "+1h"})
        assert r.status_code == 403
