"""Web-layer regressions: saved-chat ownership and the quick-reply fast-path.

Bug 1: POST /api/chats with an existing chat id kept the victim's `owner` but
deleted and re-inserted their turns — knowing a chat id meant being able to
destroy it. ChatStore.upsert now refuses cross-owner updates (returns None)
and the endpoint answers 404, the same not-found style as get/rename/delete.

Bug 2: the quick-reply fast-path in POST /api/chat stashed `tasks[run_id] =
None`, never set run_owner, and never cleaned up — so /api/stream/{run_id}
404'd for the owner and /api/admin/status 500'd (None.done AttributeError)
forever after the first greeting.

Endpoint tests drive FastAPI in-process (see docs/testing-harness.md).
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml

import web
import web.server
from web.store import ChatStore

ROOT = Path(web.__file__).resolve().parent.parent


# ---- bug 1, unit: the store-level owner check --------------------------------
def test_upsert_refuses_cross_owner_update(tmp_path):
    s = ChatStore(str(tmp_path / "chats.db"))
    s.upsert("c1", "mine", [{"user_message": "u", "answer": "a"}], owner="alice")

    out = s.upsert("c1", "pwned", [{"user_message": "x", "answer": "y"}],
                   owner="bob")
    assert out is None                                       # refused
    chat = s.get("c1", owner="alice")
    assert chat["title"] == "mine"                           # victim untouched
    assert [t["user_message"] for t in chat["turns"]] == ["u"]

    out = s.upsert("c1", "renamed",
                   [{"user_message": "u2", "answer": "a2"}], owner="alice")
    assert out is not None and out["title"] == "renamed"     # owner can update


def test_upsert_legacy_null_owner_row_stays_claimable(tmp_path):
    """Same rule as get/rename/delete: rows predating ownership (owner NULL)
    may be updated by anyone; the row keeps owner NULL on update."""
    s = ChatStore(str(tmp_path / "chats.db"))
    s.upsert("c1", "legacy", [{"user_message": "u", "answer": "a"}])   # owner NULL
    out = s.upsert("c1", "claimed", [{"user_message": "u", "answer": "a"}],
                   owner="bob")
    assert out is not None
    assert s.get("c1", owner="bob")["title"] == "claimed"


# ---- endpoint: in-process app (docs/testing-harness.md pattern) --------------
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
    app = create_app(str(base / "config" / "runtime.yaml"))

    async def fake_run(msg, **kw):      # mock the model — no LiteLLM needed
        return {}
    app.state.runtime.run = fake_run
    return app


@asynccontextmanager
async def _client(app, username="admin", password="pw"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/login", json={"username": username,
                                             "password": password})
        assert r.status_code == 200
        yield c


@pytest.mark.asyncio
async def test_save_chat_cannot_clobber_other_users_chat(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.users.create("eve", "pw2")
    async with _client(app) as c:
        r = await c.post("/api/chats", json={
            "id": "c1", "title": "mine",
            "turns": [{"user_message": "u", "answer": "a"}]})
        assert r.status_code == 200
    async with _client(app, "eve", "pw2") as c:
        r = await c.post("/api/chats", json={
            "id": "c1", "title": "pwned",
            "turns": [{"user_message": "evil", "answer": "evil"}]})
        assert r.status_code == 404                     # same as "no such chat"
        assert (await c.get("/api/chats/c1")).status_code == 404
    chat = app.state.chats.get("c1", owner="admin")     # victim's chat intact
    assert chat["title"] == "mine"
    assert [t["user_message"] for t in chat["turns"]] == ["u"]
    async with _client(app) as c:                       # owner can still update
        r = await c.post("/api/chats", json={
            "id": "c1", "title": "renamed",
            "turns": [{"user_message": "u2", "answer": "a2"}]})
        assert r.status_code == 200 and r.json()["title"] == "renamed"


@pytest.mark.asyncio
async def test_fast_path_run_is_tracked_and_streamable(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": "canned reply")
    app = _app(tmp_path, monkeypatch)
    async with _client(app) as c:
        r = await c.post("/api/chat", json={"message": "hi"})
        assert r.status_code == 200
        rid = r.json()["run_id"]
        # A real task, not None — /api/admin/status must not AttributeError.
        assert isinstance(app.state.tasks.get(rid), asyncio.Task)
        # run_owner is registered, so the owner's SSE stream is not a 404.
        # (wait_for: ASGITransport does not enforce httpx timeouts, and a
        # stream that never terminates would otherwise hang the suite.)
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
        assert r.status_code == 200
        assert "canned reply" in r.text and "run_finish" in r.text
        # Admin status survives fast-path runs (previously 500'd forever).
        assert (await c.get("/api/admin/status")).status_code == 200


@pytest.mark.asyncio
async def test_fast_path_run_state_is_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": "canned reply")
    monkeypatch.setattr(web.server, "_FORGET_AFTER_S", 0)   # cleanup immediately
    app = _app(tmp_path, monkeypatch)
    async with _client(app) as c:
        rid = (await c.post("/api/chat", json={"message": "hi"})).json()["run_id"]
        # Done-callback retires the task and the replay buffer (no leak).
        for _ in range(100):
            if rid not in app.state.tasks and rid not in app.state.bus._buffer:
                break
            await asyncio.sleep(0.02)
        assert rid not in app.state.tasks
        assert rid not in app.state.bus._buffer
