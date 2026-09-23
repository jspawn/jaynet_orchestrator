"""Role policy regression tests (Sep 2026 code audit, finding #1).

Non-admin accounts must not reach a service-user shell: `| check:` goal
commands, client-supplied auto_confirm, and the admin-only toolset
(security.admin_only_tools) are all refused/dropped server-side. The loop-level
enforcement (selection + dispatch) lives in test_loop_regressions.py; these
cover the web surface. Uses the shared web_app/web_client/record_run fixtures.
"""
from __future__ import annotations

import asyncio

import pytest


def _make_non_admin(app, username="bob", password="bobpass123"):
    app.state.users.create(username, password, is_admin=False)
    return username, password


# ---- `| check:` goal commands are admin-only ---------------------------------
@pytest.mark.asyncio
async def test_goal_check_refused_for_non_admin(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    _make_non_admin(app)
    async with web_client(app, username="bob", password="bobpass123") as c:
        r = await c.post("/api/chat", json={
            "message": "/goal build X | done when: green | check: touch /tmp/pwned"})
        assert r.status_code == 200
        rid = r.json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
        assert "admin-only" in r.text
        # The goal was NOT created — refusal, not a silent strip of the check.
        assert app.state.users.get_goal("bob") == {}


@pytest.mark.asyncio
async def test_goal_check_still_works_for_admin(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()

    async def fake_run(msg, **kw):
        return {"status": "ok", "answer": "working",
                "budget": {"tokens": {"total": 1}}}
    app.state.runtime.run = fake_run
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={
            "message": "/goal build X | done when: green | check: true"})
        assert r.status_code == 200
        rid = r.json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
        assert "admin-only" not in r.text
        goal = app.state.users.get_goal("admin")
        assert goal.get("check") == "true"
        await c.post("/api/chat", json={"message": "/goal stop"})


# ---- auto_confirm is dropped server-side for non-admins ----------------------
@pytest.mark.asyncio
async def test_auto_confirm_dropped_for_non_admin(web_app, web_client, record_run,
                                                  monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    _make_non_admin(app)
    seen = record_run(app)
    async with web_client(app, username="bob", password="bobpass123") as c:
        r = await c.post("/api/chat", json={"message": "hi", "auto_confirm": True})
        assert r.status_code == 200
        await asyncio.sleep(0.05)
    assert seen.get("auto_confirm") is False
    assert seen.get("is_admin") is False


@pytest.mark.asyncio
async def test_auto_confirm_kept_for_admin(web_app, web_client, record_run,
                                           monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    seen = record_run(app)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "hi", "auto_confirm": True})
        assert r.status_code == 200
        await asyncio.sleep(0.05)
    assert seen.get("auto_confirm") is True
    assert seen.get("is_admin") is True


# ---- slash-routed tools honor the admin-only boundary ------------------------
@pytest.mark.asyncio
async def test_slash_admin_only_tool_refused_for_non_admin(web_app, web_client):
    from runtime.tool_base import Tool

    class _OpsRun(Tool):
        name = "ops.run"
        description = "d"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, args, ctx):
            raise AssertionError("must never execute for a non-admin")

    app = web_app()
    app.state.runtime.registry.register_instance(_OpsRun())
    _make_non_admin(app)
    async with web_client(app, username="bob", password="bobpass123") as c:
        r = await c.post("/api/chat", json={"message": "/ops.run cmd=echo hi"})
        assert r.status_code == 200
        rid = r.json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
        assert "admin-only" in r.text


@pytest.mark.asyncio
async def test_slash_regular_tool_unaffected_for_non_admin(web_app, web_client):
    app = web_app()
    _make_non_admin(app)
    async with web_client(app, username="bob", password="bobpass123") as c:
        r = await c.post("/api/chat", json={"message": "/help tools"})
        assert r.status_code == 200
        rid = r.json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
        assert "admin-only" not in r.text
