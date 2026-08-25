"""Skill named in plain text → the load is pinned mechanically via
extra_system (same enforcement philosophy as /wgs and /charter). The brain
can ignore a prompt directive; it can't miss a harness-injected instruction
— seen live: j-space-loop skipped skill.load despite an explicit user
instruction AND the "Named skill? Load it." prompt directive."""
import asyncio

import pytest

from web.routes_run import _named_skill


def _spy_run(app, seen):
    async def fake_run(message, **kw):
        seen["message"] = message
        seen["extra_system"] = kw.get("extra_system")
        await kw["on_event"]({"type": "run_finish", "seq": 1})
        return {"answer": "ok"}
    app.state.runtime.run = fake_run


def test_named_skill_matching():
    names = {"j-space": {}, "tdd": {}, "wiki": {}, "deep-research": {}}
    assert _named_skill("Use the j-space skill for this task.", names) == "j-space"
    assert _named_skill("please load the TDD skill first", names) == "tdd"
    assert _named_skill("use skill deep-research on this", names) == "deep-research"
    # Bare name without the word "skill" is too noisy to force a load.
    assert _named_skill("can you use tdd here?", names) is None
    assert _named_skill("write me a wiki page", names) is None
    assert _named_skill("just a question", names) is None


@pytest.mark.asyncio
async def test_named_skill_pins_force_load(web_app, web_client):
    app = web_app(stub_run=False)
    app.state.runtime.skills = {"j-space": {}, "tdd": {}}
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        r = await c.post("/api/chat",
                         json={"message": "Use the j-space skill for this task."})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert seen, "runtime.run was never called"
    assert seen["extra_system"] is not None
    assert 'skill.load name="j-space"' in seen["extra_system"]
    assert "Skill named: j-space" in seen["extra_system"]


@pytest.mark.asyncio
async def test_unnamed_message_gets_no_nudge(web_app, web_client):
    app = web_app(stub_run=False)
    app.state.runtime.skills = {"j-space": {}}
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "just a question"})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert seen["extra_system"] is None
