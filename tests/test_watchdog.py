"""Watchdog (run coroner): trigger logic, ReportStore, the coroner's
report/fallback path, trace reconstruction, flag attach, and the
post-run hook in _launch_agent_run. No network — the app harness comes from
the shared conftest web_app/web_client fixtures."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from web import watchdog as wd
from web.store import ReportStore


# ---- ReportStore ---------------------------------------------------------------
def test_report_store(tmp_path):
    s = ReportStore(str(tmp_path / "chat.db"))
    r = s.create(run_id="r1", owner="alice", trigger="stuck", status="stuck",
                 guard_rejections=9, report="WHAT: looped\nCAUSE: x\nFIX: y")
    assert r and r["run_id"] == "r1"
    assert s.for_run("r1")["guard_rejections"] == 9
    assert s.create(run_id="r1", owner="alice", trigger="stuck",
                    status="stuck", guard_rejections=0, report="dup") is None
    assert s.count_today() == 1
    s.create(run_id="r2", owner="bob", trigger="user flag", status="ok",
             guard_rejections=0, report="note")
    assert {x["run_id"] for x in s.for_runs(["r1", "r2", "rx"])} == {"r1", "r2"}
    assert len(s.list()) == 2
    assert s.delete(r["id"]) and not s.delete(r["id"])
    assert s.delete_owner("bob") == 1
    assert s.list() == []


# ---- trigger logic ---------------------------------------------------------------
@pytest.mark.parametrize("result,cfg,want", [
    ({"status": "ok"}, {}, None),
    ({"status": "cancelled", "guard_rejections": 99}, {}, None),
    ({"status": "stuck"}, {}, "stuck"),
    ({"status": "error"}, {}, "error"),
    ({"status": "stalled"}, {}, "stalled"),
    # ok with churn = the guard working as designed — no report
    ({"status": "ok", "guard_rejections": 99}, {}, None),
    # non-ok end + churn at/over the threshold → guard churn trigger
    ({"status": "budget_exceeded", "guard_rejections": 8},
     {"min_guard_rejections": 8}, "guard churn (8 blocked duplicates)"),
    ({"status": "budget_exceeded", "guard_rejections": 3},
     {"min_guard_rejections": 8}, None),
])
def test_should_report(result, cfg, want):
    full = {**wd.DEFAULTS, **cfg}
    assert wd.should_report(result, full) == want


# ---- maybe_report / coroner -------------------------------------------------------
def _runtime(cfg=None, text="WHAT: looped\nCAUSE: vague tool\nFIX: reword", boom=False):
    async def complete(messages, think=False):
        if boom:
            raise RuntimeError("brain down")
        return {"content": text, "usage": {}}
    return SimpleNamespace(config={"watchdog": cfg or {}}, complete=complete)


@pytest.mark.asyncio
async def test_maybe_report_writes_on_stuck(tmp_path):
    s = ReportStore(str(tmp_path / "chat.db"))
    row = await wd.maybe_report(_runtime(), s, run_id="r1", owner="alice",
                                result={"status": "stuck", "error": "x",
                                        "guard_rejections": 12,
                                        "trajectory": "web.search(a)→ok"})
    assert row and row["trigger"] == "stuck" and "CAUSE" in row["report"]
    # dedupe: a second pass for the same run writes nothing
    assert await wd.maybe_report(_runtime(), s, run_id="r1", owner="alice",
                                 result={"status": "stuck"}) is None
    # ok runs are ignored entirely
    assert await wd.maybe_report(_runtime(), s, run_id="r2", owner="alice",
                                 result={"status": "ok"}) is None


@pytest.mark.asyncio
async def test_maybe_report_daily_cap_and_disabled(tmp_path):
    s = ReportStore(str(tmp_path / "chat.db"))
    rt = _runtime({"max_per_day": 1})
    r = {"status": "error", "error": "boom"}
    assert await wd.maybe_report(rt, s, run_id="r1", owner="a", result=r)
    assert await wd.maybe_report(rt, s, run_id="r2", owner="a", result=r) is None
    rt_off = _runtime({"enabled": False})
    assert await wd.maybe_report(rt_off, s, run_id="r3", owner="a",
                                 result=r) is None


@pytest.mark.asyncio
async def test_coroner_fallback_when_brain_down(tmp_path):
    s = ReportStore(str(tmp_path / "chat.db"))
    row = await wd.maybe_report(_runtime(boom=True), s, run_id="r1", owner="a",
                                result={"status": "error", "error": "kaboom",
                                        "trajectory": "fs.read(x)→error"})
    assert "coroner unavailable" in row["report"]
    assert "kaboom" in row["report"]                  # raw facts kept


# ---- trace reconstruction + flag attach -------------------------------------------
def _trace_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE runs(id TEXT PRIMARY KEY, status TEXT, error TEXT);
        CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
                            ts REAL, kind TEXT, iteration INTEGER,
                            payload_json TEXT);
    """)
    conn.execute("INSERT INTO runs VALUES('r1','stuck','guard loop')")
    conn.execute("INSERT INTO events(run_id,ts,kind,iteration,payload_json) "
                 "VALUES('r1',0,'run_finish',9,?)",
                 (json.dumps({"status": "stuck", "trajectory": "web.search(x)→ok",
                              "guard_rejections": 7,
                              "budget": {"iterations": 9}}),))
    conn.execute("INSERT INTO runs VALUES('r2','error','kaboom')")
    conn.commit()
    conn.close()
    return str(path)


def test_result_from_trace(tmp_path):
    db = _trace_db(tmp_path / "trace.db")
    r = wd.result_from_trace(db, "r1")
    assert r["status"] == "stuck" and r["guard_rejections"] == 7
    assert "web.search" in r["trajectory"]
    assert wd.result_from_trace(db, "nope") is None


@pytest.mark.asyncio
async def test_attach_to_flag(tmp_path):
    db = _trace_db(tmp_path / "trace.db")
    s = ReportStore(str(tmp_path / "chat.db"))
    s.create(run_id="r1", owner="alice", trigger="stuck", status="stuck",
             guard_rejections=7, report="already there")
    written = await wd.attach_to_flag(_runtime(), s, db, "alice",
                                      ["r1", "r2", "unknown"])
    assert written == 1                                # only r2 (r1 deduped)
    row = s.for_run("r2")
    assert row["trigger"] == "user flag" and row["status"] == "error"


# ---- the post-run hook (app harness from conftest) -----------------------------
@pytest.mark.asyncio
async def test_distressed_run_gets_coroner_report(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()

    async def fake_run(msg, **kw):
        return {"status": "stuck", "answer": "", "error": "guard loop",
                "guard_rejections": 11, "trajectory": "web.search(x)→ok",
                "budget": {"iterations": 9, "tokens": {"total": 500}}}
    app.state.runtime.run = fake_run

    async def coroner(messages, think=False):
        return {"content": "WHAT: looped on search\nCAUSE: vague\nFIX: reword",
                "usage": {}}
    app.state.runtime.complete = coroner

    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "loop please"})
        assert r.status_code == 200
        rid = r.json()["run_id"]
        row = None
        for _ in range(200):
            row = app.state.reports.for_run(rid)
            if row:
                break
            await asyncio.sleep(0.05)
        assert row, "no coroner report was written"
        assert row["trigger"] == "stuck" and row["guard_rejections"] == 11
        assert "CAUSE" in row["report"]
        # admin endpoints list + delete it
        d = (await c.get("/api/admin/reports")).json()
        assert any(x["run_id"] == rid for x in d["reports"])
        assert (await c.delete(f"/api/admin/reports/{row['id']}")).status_code == 200


@pytest.mark.asyncio
async def test_healthy_run_gets_no_report(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()

    async def fake_run(msg, **kw):
        return {"status": "ok", "answer": "fine", "guard_rejections": 0,
                "budget": {}}
    app.state.runtime.run = fake_run

    async with web_client(app) as c:
        await c.post("/api/chat", json={"message": "all good"})
        for _ in range(50):
            await asyncio.sleep(0.05)
        assert app.state.reports.list() == []
