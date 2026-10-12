"""Audit 2026-10-12 Medium #1: deleting a user must remove their live state —
schedule entries, owner-scoped knowledge rows (memory/kg/rag, the pageindex
dir) and trace runs — and a recreated same-name account must not validate the
deleted account's session cookies (session_epoch starts random, not 0).

Endpoint tests drive FastAPI in-process (see docs/testing-harness.md).
"""
import itertools
import os
import sqlite3
import time
from types import SimpleNamespace

import httpx
import pytest

import web.auth
from runtime.scheduler import ScheduleStore
from runtime.trace import Trace, delete_owner
from tools.kg import graph as kg_store
from tools.memory import store as mem_store
from tools.rag import store as rag_store


def _wire_owner_state(app, tmp_path):
    """Point the app's owner-scoped stores at tmp_path and seed rows for
    'eve' (to be deleted) and 'bob' (must survive). Returns the paths."""
    cfg = app.state.runtime.config.setdefault("tools", {})
    memory_db = str(tmp_path / "memory.db")
    rag_db = str(tmp_path / "rag.db")
    sched_path = str(tmp_path / "schedules.json")
    cfg["memory"] = {"db_path": memory_db}
    cfg["kg"] = {}                                   # kg shares the memory DB
    cfg["rag"] = {"db_path": rag_db}
    cfg["schedule"] = {"store": sched_path}

    conn = sqlite3.connect(memory_db)
    mem_store._ensure_schema(conn)
    kg_store._ensure_schema(conn)
    for owner in ("eve", "bob"):
        conn.execute("INSERT INTO memory(ts, epoch, content, owner) "
                     "VALUES ('t', 0, ?, ?)", (f"{owner} note", owner))
        conn.execute("INSERT INTO kg_entity(owner, name, ts) VALUES (?, ?, 't')",
                     (owner, f"{owner}-thing"))
    conn.commit()
    conn.close()

    ctx = SimpleNamespace(config={"tools": {"rag": {"db_path": rag_db}}})
    conn = rag_store._db(ctx)
    for owner in ("eve", "bob"):
        conn.execute("INSERT INTO rag_doc(collection, text, dim, embedding, ts,"
                     " owner) VALUES ('c', ?, 0, X'00', 't', ?)",
                     (f"{owner} chunk", owner))
    conn.commit()
    conn.close()

    store = ScheduleStore(sched_path)
    for owner in ("eve", "bob"):
        store.add({"owner": owner, "prompt": f"{owner} job", "kind": "once",
                   "next_fire": time.time() + 3600})

    tr = Trace(app.state.runtime.config["trace"]["db_path"])
    for owner in ("eve", "bob"):
        tr.start_run(f"run-{owner}", "hello", owner=owner)
        tr.log(f"run-{owner}", "tool_call", 1, {"tool": "x"})
        tr.finish_run(f"run-{owner}", "ok")
    tr.close()

    from runtime import paths
    pi_dir = paths.DATA / "pageindex" / "eve"
    pi_dir.mkdir(parents=True)
    (pi_dir / "idx.json").write_text("{}")
    return memory_db, rag_db, store, pi_dir


@pytest.mark.asyncio
async def test_delete_user_removes_schedules_knowledge_and_traces(
        tmp_path, web_app, web_client):
    app = web_app()
    app.state.users.create("eve", "pw2")
    app.state.users.create("bob", "pw3")
    memory_db, rag_db, store, pi_dir = _wire_owner_state(app, tmp_path)

    async with web_client(app) as c:                        # admin
        r = await c.delete("/api/admin/users/eve")
    assert r.status_code == 200
    removed = r.json()["state_removed"]
    assert removed["schedules"] == 1
    assert removed["memory"] == 1
    assert removed["kg"] == 1
    assert removed["rag"] == 1
    assert removed["trace_runs"] == 1
    from runtime import paths as _paths
    assert removed.get("pageindex_dir") == str(pi_dir), (
        f"removed={removed} plugins_cfg={app.state.runtime.config.get('plugins')} "
        f"DATA={_paths.DATA} env_DATA={os.environ.get('JAYNET_DATA')} "
        f"pi_exists={pi_dir.exists()}")

    # eve's schedule is gone — nothing left for the tick to fire — while
    # bob's entry is untouched.
    assert [e["owner"] for e in store.list()] == ["bob"]

    conn = sqlite3.connect(memory_db)
    assert [r[0] for r in conn.execute(
        "SELECT owner FROM memory ORDER BY owner")] == ["bob"]
    assert [r[0] for r in conn.execute(
        "SELECT owner FROM kg_entity ORDER BY owner")] == ["bob"]
    conn.close()
    conn = sqlite3.connect(rag_db)
    assert [r[0] for r in conn.execute(
        "SELECT owner FROM rag_doc ORDER BY owner")] == ["bob"]
    conn.close()
    conn = sqlite3.connect(app.state.runtime.config["trace"]["db_path"])
    assert [r[0] for r in conn.execute(
        "SELECT owner FROM runs ORDER BY owner")] == ["bob"]
    assert [r[0] for r in conn.execute(
        "SELECT run_id FROM events ORDER BY run_id")] == ["run-bob"]
    conn.close()
    assert not pi_dir.exists()                   # eve's index dir is gone

    # A deleted user's trace/knowledge cleanup doesn't touch ownerless CLI rows.
    conn = sqlite3.connect(memory_db)
    conn.execute("INSERT INTO memory(ts, epoch, content, owner) "
                 "VALUES ('t', 0, 'cli note', '')")
    conn.commit()
    conn.close()
    app.state.users.create("eve", "pw2")
    async with web_client(app) as c:
        r = await c.delete("/api/admin/users/eve")
    assert r.status_code == 200
    conn = sqlite3.connect(memory_db)
    assert [r[0] for r in conn.execute(
        "SELECT owner FROM memory ORDER BY owner")] == ["", "bob"]
    conn.close()


@pytest.mark.asyncio
async def test_delete_user_tolerates_missing_stores(tmp_path, web_app,
                                                    web_client):
    """No memory/rag/schedule files yet (fresh install): the delete still
    succeeds and reports zeros."""
    app = web_app()
    app.state.users.create("eve", "pw2")
    cfg = app.state.runtime.config.setdefault("tools", {})
    cfg["memory"] = {"db_path": str(tmp_path / "no-memory.db")}
    cfg["rag"] = {"db_path": str(tmp_path / "no-rag.db")}
    cfg["schedule"] = {"store": str(tmp_path / "no-schedules.json")}
    async with web_client(app) as c:
        r = await c.delete("/api/admin/users/eve")
    assert r.status_code == 200
    removed = r.json()["state_removed"]
    assert removed["schedules"] == removed["memory"] == removed["rag"] == 0
    assert not (tmp_path / "no-memory.db").exists()   # cleanup never creates DBs


def test_new_accounts_start_at_random_session_epoch(tmp_path, monkeypatch):
    counter = itertools.count(1)
    monkeypatch.setattr(web.auth.secrets, "randbelow", lambda n: next(counter) * 7)
    us = web.auth.UserStore(str(tmp_path / "users.db"))
    us.create("a", "password1")
    us.create("b", "password2")
    ea, eb = us.session_epoch("a"), us.session_epoch("b")
    assert ea > 0 and eb > 0 and ea != eb
    # password change / logout-everywhere still bump from the random base
    assert us.bump_session_epoch("a") == ea + 1


@pytest.mark.asyncio
async def test_recreated_account_rejects_deleted_accounts_cookie(
        tmp_path, web_app, web_client, monkeypatch):
    counter = itertools.count(1)
    monkeypatch.setattr(web.auth.secrets, "randbelow",
                        lambda n: 1000 * next(counter))
    app = web_app()
    app.state.users.create("eve", "pw2")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ec:
        r = await ec.post("/api/login", json={"username": "eve",
                                              "password": "pw2"})
        assert r.status_code == 200
        old_cookie = r.cookies.get("jaynet_session")
        assert old_cookie
        assert (await ec.get("/api/me")).status_code == 200

        async with web_client(app) as admin:
            assert (await admin.delete("/api/admin/users/eve")).status_code == 200
        # Deleted: the cookie (still in this client's jar) authenticates no one.
        assert (await ec.get("/api/me")).status_code == 401
        # Recreated under the same name: the OLD cookie still must not work —
        # the new account started at a fresh random session epoch.
        app.state.users.create("eve", "pw2")
        assert (await ec.get("/api/me")).status_code == 401
        # ...while a fresh login works normally.
        r = await ec.post("/api/login", json={"username": "eve",
                                              "password": "pw2"})
        assert r.status_code == 200
        assert (await ec.get("/api/me")).status_code == 200


# ---- store-level helpers ----------------------------------------------------
def test_trace_delete_owner_removes_runs_and_events(tmp_path):
    db = str(tmp_path / "trace.db")
    tr = Trace(db)
    tr.start_run("r1", "m", owner="eve")
    tr.log("r1", "tool_call", 1, {"tool": "x"})
    tr.finish_run("r1", "ok")
    tr.start_run("r2", "m")                        # ownerless CLI run
    tr.finish_run("r2", "ok")
    tr.close()
    assert delete_owner(db, "eve") == 1
    conn = sqlite3.connect(db)
    assert [r[0] for r in conn.execute("SELECT id FROM runs")] == ["r2"]
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    conn.close()
    assert delete_owner(str(tmp_path / "missing.db"), "eve") == 0


def test_schedule_store_remove_owner(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.json"))
    s.add({"owner": "eve", "prompt": "a", "kind": "once", "next_fire": 1.0})
    s.add({"owner": "eve", "prompt": "b", "kind": "every", "every_s": 60,
           "next_fire": 1.0})
    s.add({"owner": "bob", "prompt": "c", "kind": "once", "next_fire": 1.0})
    assert s.remove_owner("eve") == 2
    assert [e["owner"] for e in s.list()] == ["bob"]
    assert s.remove_owner("eve") == 0
    # persists across instances
    assert len(ScheduleStore(str(tmp_path / "s.json")).list()) == 1
