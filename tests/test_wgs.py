"""/wgs slash command: rewrites itself into a normal agent run with the
writing-great-skills playbook force-loaded via extra_system (a forced-load
pointer), instead of hitting the no-model slash fast-path."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _app(tmp_path, monkeypatch):
    base = tmp_path
    (base / "config").mkdir()
    (base / "prompts").mkdir()
    cfg = yaml.safe_load(open(ROOT / "config/runtime.yaml"))
    cfg["trace"]["db_path"] = str(base / "trace.db")
    cfg["orchestrator"]["system_prompt"] = "prompts/orchestrator.md"
    cfg["web"] = {"chats_db": str(base / "chats.db"),
                  "users_db": str(base / "users.db"),
                  "outputs_dir": str(base / "outputs"),
                  "projects_dir": str(base / "projects")}
    (base / "prompts" / "orchestrator.md").write_text("P")
    yaml.safe_dump(cfg, open(base / "config" / "runtime.yaml", "w"))
    monkeypatch.setenv("ORCH_ADMIN_USER", "admin")
    monkeypatch.setenv("ORCH_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("ORCH_SESSION_SECRET", "t")
    from web.server import create_app
    return create_app(str(base / "config" / "runtime.yaml"))


@asynccontextmanager
async def _client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert r.status_code == 200
        yield c


def _spy_run(app, seen):
    async def fake_run(message, **kw):
        seen["message"] = message
        seen["extra_system"] = kw.get("extra_system")
        await kw["on_event"]({"type": "run_finish", "seq": 1})
        return {"answer": "ok"}
    app.state.runtime.run = fake_run


@pytest.mark.asyncio
async def test_wgs_rewrites_into_agent_run_with_playbook(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    seen = {}
    _spy_run(app, seen)
    async with _client(app) as c:
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
async def test_wgs_bare_uses_default_prompt(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    seen = {}
    _spy_run(app, seen)
    async with _client(app) as c:
        r = await c.post("/api/chat", json={"message": "/wgs"})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert seen["message"].startswith("I want to write or improve a skill")
    assert "writing-great-skills" in seen["extra_system"]


@pytest.mark.asyncio
async def test_normal_run_passes_no_extra_system(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    seen = {}
    _spy_run(app, seen)
    async with _client(app) as c:
        r = await c.post("/api/chat", json={"message": "just a question"})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert seen["extra_system"] is None
