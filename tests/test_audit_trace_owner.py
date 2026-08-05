"""Audit S2: trace.query / trace.mine are owner-scoped by default.

Policy (tools/trace/owner.py): a caller with ctx.owner sees only their own
runs (unowned system/scheduled runs are invisible to them); all_owners=true is
the admin/debug escape hatch; an ownerless ctx (CLI/token path — the trusted
local operator) is unfiltered.
"""
import json
import sqlite3
import time

from conftest import run

from tools.trace.mine import TraceMine
from tools.trace.query import TraceQuery


def _seed(tmp_path):
    """Three runs: alice's, bob's, and an unowned (system/scheduled) one."""
    db = tmp_path / "trace.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, started_at REAL, finished_at REAL,"
        " owner TEXT, user_message TEXT, final_answer TEXT, status TEXT, error TEXT,"
        " total_tokens INTEGER, cost_usd REAL, summary_json TEXT);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,"
        " ts REAL, kind TEXT, iteration INTEGER, payload_json TEXT);"
    )
    now = time.time()
    for rid, owner, msg in (("R-ALICE", "alice", "alice secret prompt"),
                            ("R-BOB", "bob", "bob secret prompt"),
                            ("R-BOB2", "bob", "bob second run"),
                            ("R-SYS", None, "system prompt")):
        conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (rid, now, now, owner, msg, "done", "ok", None, 1, 0.0, None))
        conn.execute("INSERT INTO events (run_id, ts, kind, iteration, payload_json)"
                     " VALUES (?,?,?,?,?)",
                     (rid, now, "tool_call", 1,
                      json.dumps({"tool": "fs.read", "args": f"{owner} args"})))
        conn.execute("INSERT INTO events (run_id, ts, kind, iteration, payload_json)"
                     " VALUES (?,?,?,?,?)",
                     (rid, now, "error", 1,
                      json.dumps({"error": f"{owner} boom"})))
    conn.commit()
    conn.close()
    return str(db)


def _cfg(db):
    return {"tools": {}, "trace": {"db_path": db}}


# ---- trace.query --------------------------------------------------------------

def test_query_runs_scoped_to_caller(tmp_path, ctx):
    cfg = _cfg(_seed(tmp_path))
    r = run(TraceQuery().execute({"view": "runs"}, ctx(config=cfg, owner="alice")))
    ids = [x["run_id"] for x in r.result["runs"]]
    assert ids == ["R-ALICE"]                     # not bob's, not the system run


def test_query_runs_all_owners_escape_hatch(tmp_path, ctx):
    cfg = _cfg(_seed(tmp_path))
    r = run(TraceQuery().execute({"view": "runs", "all_owners": True},
                                 ctx(config=cfg, owner="alice")))
    ids = sorted(x["run_id"] for x in r.result["runs"])
    assert ids == ["R-ALICE", "R-BOB", "R-BOB2", "R-SYS"]


def test_query_runs_ownerless_ctx_unfiltered(tmp_path, ctx):
    # CLI/token path: the trusted local operator keeps the historical behavior.
    cfg = _cfg(_seed(tmp_path))
    r = run(TraceQuery().execute({"view": "runs"}, ctx(config=cfg)))
    assert r.result["count"] == 4


def test_query_events_cannot_read_another_users_run(tmp_path, ctx):
    cfg = _cfg(_seed(tmp_path))
    r = run(TraceQuery().execute({"view": "events", "run_id": "R-BOB"},
                                 ctx(config=cfg, owner="alice")))
    assert r.status == "ok" and r.result["count"] == 0
    r = run(TraceQuery().execute({"view": "events", "run_id": "R-BOB",
                                  "all_owners": True},
                                 ctx(config=cfg, owner="alice")))
    assert r.result["count"] == 2
    # ...and your own run still works without the escape hatch.
    r = run(TraceQuery().execute({"view": "events", "run_id": "R-ALICE"},
                                 ctx(config=cfg, owner="alice")))
    assert r.result["count"] == 2


def test_query_failures_scoped(tmp_path, ctx):
    cfg = _cfg(_seed(tmp_path))
    r = run(TraceQuery().execute({"view": "failures"}, ctx(config=cfg, owner="bob")))
    assert r.result["count"] == 2
    assert all("bob boom" in str(f["error"]) for f in r.result["failures"])
    r = run(TraceQuery().execute({"view": "failures", "all_owners": True},
                                 ctx(config=cfg, owner="bob")))
    assert r.result["count"] == 4


# ---- trace.mine ---------------------------------------------------------------

def test_mine_scoped_to_caller(tmp_path, ctx):
    cfg = _cfg(_seed(tmp_path))
    r = run(TraceMine().execute({"min_count": 1}, ctx(config=cfg, owner="alice")))
    assert r.status == "ok" and r.result["runs_analyzed"] == 1


def test_mine_all_owners_escape_hatch(tmp_path, ctx):
    cfg = _cfg(_seed(tmp_path))
    r = run(TraceMine().execute({"min_count": 1, "all_owners": True},
                                ctx(config=cfg, owner="alice")))
    assert r.result["runs_analyzed"] == 4


def test_mine_explicit_owner_cannot_pivot(tmp_path, ctx):
    # A web caller asking for someone else's owner still gets only their own
    # (1 run — pivoting to bob would see 2).
    cfg = _cfg(_seed(tmp_path))
    r = run(TraceMine().execute({"min_count": 1, "owner": "bob"},
                                ctx(config=cfg, owner="alice")))
    assert r.result["runs_analyzed"] == 1
    # With all_owners the explicit owner narrows (admin debugging).
    r = run(TraceMine().execute({"min_count": 1, "owner": "bob",
                                 "all_owners": True},
                                ctx(config=cfg, owner="alice")))
    assert r.result["runs_analyzed"] == 2
