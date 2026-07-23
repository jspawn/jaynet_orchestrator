"""/wgs slash command: rewrites itself into a normal agent run with the
writing-great-skills playbook force-loaded via extra_system (a forced-load
pointer), instead of hitting the no-model slash fast-path."""
import asyncio

import pytest


def _spy_run(app, seen):
    async def fake_run(message, **kw):
        seen["message"] = message
        seen["extra_system"] = kw.get("extra_system")
        await kw["on_event"]({"type": "run_finish", "seq": 1})
        return {"answer": "ok"}
    app.state.runtime.run = fake_run


@pytest.mark.asyncio
async def test_wgs_rewrites_into_agent_run_with_playbook(web_app, web_client):
    app = web_app(stub_run=False)
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "/wgs improve the tdd skill"})
        rid = r.json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    # Went through the agent loop (not the no-model slash fast-path) …
    assert seen, "runtime.run was never called — /wgs hit the slash fast-path"
    # … with the command stripped down to the topic …
    assert seen["message"] == "improve the tdd skill"
    # … and the playbook force-loaded via extra_system.
    assert "writing-great-skills" in seen["extra_system"]
    assert "skill.load" in seen["extra_system"]


@pytest.mark.asyncio
async def test_wgs_bare_uses_default_prompt(web_app, web_client):
    app = web_app(stub_run=False)
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "/wgs"})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert seen["message"].startswith("I want to write or improve a skill")
    assert "writing-great-skills" in seen["extra_system"]


@pytest.mark.asyncio
async def test_normal_run_passes_no_extra_system(web_app, web_client):
    app = web_app(stub_run=False)
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "just a question"})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert seen["extra_system"] is None
