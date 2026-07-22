"""Flag-for-debugging: a user marks a broken session, the admin reviews a
privacy-safe structural log in the Flags tab.

Design under test:
- FlagStore (own table in the chats DB file) holds only metadata + the user's
  comment — never content.
- POST /api/flag keeps only runs that actually belong to the caller (token
  sessions match the NULL-owner rows their runs are traced with).
- GET /api/admin/flags/{id} returns run metadata (status/error/tokens/timing —
  NOT user_message/final_answer) and events with every content-bearing field
  stripped (same key set as Trace._strip_content).
- Non-admins are kept off /api/admin/flags by the admin middleware (403).
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web.store import FlagStore  # noqa: E402
from runtime.trace import Trace  # noqa: E402


# ---- store-level ------------------------------------------------------------
def test_store_roundtrip_resolve_delete(tmp_path):
    s = FlagStore(str(tmp_path / "c.db"))
    f = s.create("alice", ["a" * 32, "b" * 32], comment="tool calls failed",
                 conversation_id="cid", chat_title="broken chat")
    assert f["resolved"] is False and f["run_ids"] == ["a" * 32, "b" * 32]
    got = s.get(f["id"])
    assert got["owner"] == "alice" and got["comment"] == "tool calls failed"
    assert s.set_resolved(f["id"], True) is True
    assert s.get(f["id"])["resolved"] is True
    assert [x["id"] for x in s.list(include_resolved=False)] == []
    assert [x["id"] for x in s.list()] == [f["id"]]
    assert s.delete(f["id"]) is True and s.get(f["id"]) is None


def test_store_delete_owner(tmp_path):
    s = FlagStore(str(tmp_path / "c.db"))
    s.create("alice", ["a" * 32])
    s.create("bob", ["b" * 32])
    assert s.delete_owner("alice") == 1
    assert [f["owner"] for f in s.list()] == ["bob"]


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


def _seed_run(db_path, run_id, owner, status="error", error="boom"):
    t = Trace(db_path, log_content=True)
    t.start_run(run_id, "my secret question", owner=owner)
    t.log(run_id, "tool_call", 1, {"name": "fs.read",
                                   "args": {"path": "/secret/file.txt"}})
    t.log(run_id, "tool_result", 1, {"name": "fs.read",
                                     "result": "secret file contents"})
    t.log(run_id, "error", 1, {"error": error})
    t.finish_run(run_id, status, final_answer="secret answer", error=error,
                 summary={"tokens": {"total": 123}, "cost_usd": 0.001})
    t.close()


@pytest.mark.asyncio
async def test_flag_flow_privacy_safe(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    db = str(tmp_path / "trace.db")
    rid = "a" * 32
    _seed_run(db, rid, owner="admin")
    async with _client(app) as c:
        r = await c.post("/api/flag", json={
            "comment": "lots of failed tool calls", "chat_title": "broken",
            "conversation_id": "cid1", "run_ids": [rid, "f" * 32, "forged"]})
        assert r.status_code == 200
        body = r.json()
        assert body["runs"] == 1            # unknown + malformed ids dropped
        fid = body["flag_id"]

        listed = (await c.get("/api/admin/flags")).json()["flags"]
        assert [f["id"] for f in listed] == [fid]
        assert listed[0]["owner"] == "admin"
        assert listed[0]["comment"] == "lots of failed tool calls"

        d = (await c.get(f"/api/admin/flags/{fid}")).json()
        assert len(d["runs"]) == 1
        run = d["runs"][0]
        # Metadata kept for debugging …
        assert run["status"] == "error" and run["error"] == "boom"
        assert run["total_tokens"] == 123 and run["duration_s"] is not None
        # … but no content anywhere: not the run row, not the events.
        assert "user_message" not in run and "final_answer" not in run
        blob = str(d)
        for leaked in ("my secret question", "secret answer",
                       "secret file contents", "/secret/file.txt"):
            assert leaked not in blob
        kinds = [e["kind"] for e in run["events"]]
        assert "tool_call" in kinds and "tool_result" in kinds
        call = next(e for e in run["events"] if e["kind"] == "tool_call")
        assert call["payload"]["name"] == "fs.read"          # structure kept
        assert call["payload"]["args"] == "<stripped>"       # content gone

        # resolve → reopen → delete
        assert (await c.post(f"/api/admin/flags/{fid}/resolve",
                             json={"resolved": True})).status_code == 200
        assert (await c.get("/api/admin/flags")).json()["flags"][0]["resolved"]
        assert (await c.delete(f"/api/admin/flags/{fid}")).status_code == 200
        assert (await c.get("/api/admin/flags")).json()["flags"] == []


@pytest.mark.asyncio
async def test_flag_rejects_other_users_runs(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.users.create("eve", "pw2")
    db = str(tmp_path / "trace.db")
    _seed_run(db, "a" * 32, owner="admin")
    _seed_run(db, "b" * 32, owner="eve")
    async with _client(app, "eve", "pw2") as c:
        r = await c.post("/api/flag", json={"run_ids": ["a" * 32]})
        assert r.status_code == 400            # admin's run is not hers
        r = await c.post("/api/flag", json={"run_ids": ["b" * 32, "a" * 32]})
        assert r.status_code == 200 and r.json()["runs"] == 1
        # … and she is kept off the admin endpoints entirely.
        assert (await c.get("/api/admin/flags")).status_code == 403


@pytest.mark.asyncio
async def test_flag_token_session_matches_null_owner(tmp_path, monkeypatch):
    """Token sessions run with trace owner NULL; their flags must match those
    rows (same convention as the shared '_token' current-chat row)."""
    app = _app(tmp_path, monkeypatch)
    db = str(tmp_path / "trace.db")
    _seed_run(db, "c" * 32, owner=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 headers={"authorization": "Bearer tok"}) as c:
        r = await c.post("/api/flag", json={"run_ids": ["c" * 32]})
        assert r.status_code == 200 and r.json()["runs"] == 1
        flags = (await c.get("/api/admin/flags")).json()["flags"]
        assert flags[0]["owner"] == "_token"


@pytest.mark.asyncio
async def test_flag_empty_or_unknown_runs(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with _client(app) as c:
        assert (await c.post("/api/flag", json={"run_ids": []})).status_code == 400
        assert (await c.post("/api/flag",
                             json={"run_ids": ["e" * 32]})).status_code == 400
