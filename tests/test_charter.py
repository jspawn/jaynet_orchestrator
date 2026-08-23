"""/charter slash command: rewrites itself into a normal agent run with the
project-charter skill force-loaded via extra_system and the project wiki dir
granted as an extra writable root. Requires an active project — without one
the run is told to say so and write nothing."""
import asyncio

import pytest

from web import projects as PJ


def _spy_run(app, seen):
    async def fake_run(message, **kw):
        seen["message"] = message
        seen["extra_system"] = kw.get("extra_system")
        seen["extra_roots"] = kw.get("extra_roots")
        await kw["on_event"]({"type": "run_finish", "seq": 1})
        return {"answer": "ok"}
    app.state.runtime.run = fake_run


@pytest.mark.asyncio
async def test_charter_rewrites_into_agent_run(web_app, web_client, tmp_path):
    app = web_app(stub_run=False)
    seen = {}
    _spy_run(app, seen)
    meta = PJ.create_project(tmp_path / "projects", "admin", "Demo")
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "/charter it's a bot",
                                            "project_id": meta["id"]})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    # Went through the agent loop (not the no-model slash fast-path) …
    assert seen, "runtime.run was never called — /charter hit the slash fast-path"
    # … with the command stripped down to the note (the project-context
    # prefix is added later by _augment_with_project) …
    assert seen["message"].endswith("it's a bot")
    # … the playbook force-loaded via extra_system …
    assert 'skill.load name="project-charter"' in seen["extra_system"]
    # … and the project wiki granted as the one extra writable root.
    wiki = tmp_path / "projects" / "admin" / meta["id"] / "files" / "wiki"
    assert seen["extra_roots"] == [str(wiki)]
    assert str(wiki) in seen["extra_system"]


@pytest.mark.asyncio
async def test_charter_bare_uses_default_prompt(web_app, web_client, tmp_path):
    app = web_app(stub_run=False)
    seen = {}
    _spy_run(app, seen)
    meta = PJ.create_project(tmp_path / "projects", "admin", "Demo")
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "/charter",
                                            "project_id": meta["id"]})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert "Start the charter interview" in seen["message"]
    assert 'skill.load name="project-charter"' in seen["extra_system"]


@pytest.mark.asyncio
async def test_charter_without_project_writes_nothing(web_app, web_client):
    app = web_app(stub_run=False)
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "/charter"})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert seen["extra_roots"] is None
    assert "no project wiki" in seen["extra_system"]
    assert "do not write any files" in seen["extra_system"]
