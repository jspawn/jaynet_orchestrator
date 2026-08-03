"""/llmwiki slash command: rewrites itself into a normal agent run with the
wiki skill force-loaded via extra_system and the wiki dir granted as an extra
writable root — project-scoped (deleted with the project) in a project chat,
the owner's global wiki otherwise."""
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
async def test_llmwiki_rewrites_into_agent_run(web_app, web_client, tmp_path):
    app = web_app(stub_run=False)
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        r = await c.post("/api/chat",
                         json={"message": "/llmwiki add a page about SBB tickets"})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    # Went through the agent loop (not the no-model slash fast-path) …
    assert seen, "runtime.run was never called — /llmwiki hit the slash fast-path"
    # … with the command stripped down to the request …
    assert seen["message"] == "add a page about SBB tickets"
    # … the playbook force-loaded via extra_system …
    assert 'skill.load name="wiki"' in seen["extra_system"]
    # … and the owner's global wiki granted as the one extra writable root.
    wiki = str(tmp_path / "wiki" / "admin")
    assert seen["extra_roots"] == [wiki]
    assert wiki in seen["extra_system"]


@pytest.mark.asyncio
async def test_llmwiki_bare_uses_default_prompt(web_app, web_client):
    app = web_app(stub_run=False)
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "/llmwiki"})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert seen["message"].startswith("Show me the current state of the wiki")
    assert 'skill.load name="wiki"' in seen["extra_system"]


@pytest.mark.asyncio
async def test_llmwiki_project_scoped_wiki(web_app, web_client, tmp_path):
    app = web_app(stub_run=False)
    seen = {}
    _spy_run(app, seen)
    meta = PJ.create_project(tmp_path / "projects", "admin", "Demo")
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "/llmwiki status",
                                            "project_id": meta["id"]})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    # The wiki lives INSIDE the project's files dir — deleted with the project.
    wiki = tmp_path / "projects" / "admin" / meta["id"] / "files" / "wiki"
    assert seen["extra_roots"] == [str(wiki)]
    assert wiki.is_dir()


@pytest.mark.asyncio
async def test_normal_run_gets_no_extra_roots(web_app, web_client):
    app = web_app(stub_run=False)
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "just a question"})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert seen["extra_roots"] is None
    assert seen["extra_system"] is None
