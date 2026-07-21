"""Current-chat sync: the active (possibly unsaved) chat follows the user.

Previously the in-progress chat lived only in one browser's localStorage, so
two devices had two independent sessions. ChatStore now keeps one
`current_chat` row per owner (payload stored verbatim, last writer wins) and
the web client syncs to it — the server copy is authoritative, localStorage
degrades to an offline fallback. Token sessions (ORCH_WEB_TOKEN bearer) share
the "_token" row, same convention as _owner_dir.

Covered here:
- store roundtrip / replace / clear / per-owner isolation / user deletion
- PUT→GET endpoint roundtrip and cross-user isolation
- PUT chat:null and DELETE both clear the snapshot (the client's "new chat")
- garbage active_run is rejected (server-minted ids only, like run_id)
- bearer-token sessions read/write the shared "_token" row
"""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web.store import ChatStore  # noqa: E402

SNAP = {"id": None, "cid": "abc", "title": None, "saved": False,
        "turns": [{"user_message": "hi", "answer": "yo", "run_id": None,
                   "status": "ok", "events": []}]}


# ---- store-level ------------------------------------------------------------
def test_current_roundtrip_and_replace(tmp_path):
    s = ChatStore(str(tmp_path / "c.db"))
    assert s.get_current("alice") is None
    s.set_current("alice", SNAP, active_run=None)
    got = s.get_current("alice")
    assert got["chat"] == SNAP and got["active_run"] is None
    newer = {**SNAP, "turns": SNAP["turns"] * 2}
    s.set_current("alice", newer, active_run="a" * 32)
    got = s.get_current("alice")                       # full replace, no merge
    assert len(got["chat"]["turns"]) == 2 and got["active_run"] == "a" * 32


def test_current_isolated_per_owner(tmp_path):
    s = ChatStore(str(tmp_path / "c.db"))
    s.set_current("alice", SNAP)
    assert s.get_current("bob") is None                 # no cross-user leakage


def test_current_clear_and_delete_owner(tmp_path):
    s = ChatStore(str(tmp_path / "c.db"))
    s.set_current("alice", SNAP)
    s.set_current("bob", SNAP)
    assert s.clear_current("alice") is True
    assert s.get_current("alice") is None
    s.upsert("c1", "t", [{"user_message": "u", "answer": "a"}], owner="bob")
    s.delete_owner("bob")                               # user deletion wipes both
    assert s.get_current("bob") is None
    assert s.get("c1", owner="bob") is None


# ---- endpoints: in-process app (docs/testing-harness.md pattern) ------------
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
    monkeypatch.setenv("ORCH_WEB_TOKEN", "tok")
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
async def test_put_get_roundtrip_and_clear(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with _client(app) as c:
        r = await c.get("/api/current-chat")
        assert r.status_code == 200 and r.json()["chat"] is None
        rid = "b" * 32
        r = await c.put("/api/current-chat",
                        json={"chat": SNAP, "active_run": rid})
        assert r.status_code == 200
        got = (await c.get("/api/current-chat")).json()
        assert got["chat"] == SNAP and got["active_run"] == rid
        assert got["updated_at"]
        r = await c.put("/api/current-chat", json={"chat": None})
        assert r.status_code == 200
        assert (await c.get("/api/current-chat")).json()["chat"] is None
        # Re-sync, then DELETE clears as well.
        await c.put("/api/current-chat", json={"chat": SNAP})
        assert (await c.delete("/api/current-chat")).status_code == 200
        assert (await c.get("/api/current-chat")).json()["chat"] is None


@pytest.mark.asyncio
async def test_current_chat_isolated_between_users(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.users.create("eve", "pw2")
    async with _client(app) as c:
        assert (await c.put("/api/current-chat",
                            json={"chat": SNAP})).status_code == 200
    async with _client(app, "eve", "pw2") as c:
        assert (await c.get("/api/current-chat")).json()["chat"] is None
    async with _client(app) as c:                        # admin's copy intact
        assert (await c.get("/api/current-chat")).json()["chat"] == SNAP


@pytest.mark.asyncio
async def test_active_run_must_be_server_minted(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with _client(app) as c:
        r = await c.put("/api/current-chat",
                        json={"chat": SNAP, "active_run": "../../etc"})
        assert r.status_code == 422
        r = await c.put("/api/current-chat",
                        json={"chat": {"turns": "nope"}})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_bearer_token_shares_token_row(tmp_path, monkeypatch):
    """ORCH_WEB_TOKEN sessions have no account; they all share the "_token"
    row (like _owner_dir), so a token-only single-user deployment still gets
    cross-device sync."""
    app = _app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 headers={"authorization": "Bearer tok"}) as c:
        assert (await c.put("/api/current-chat",
                            json={"chat": SNAP})).status_code == 200
        assert (await c.get("/api/current-chat")).json()["chat"] == SNAP
    # ...and it does NOT leak into a named user's row.
    async with _client(app) as c:
        assert (await c.get("/api/current-chat")).json()["chat"] is None
    assert app.state.chats.get_current("_token")["chat"] == SNAP
