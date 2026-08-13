"""Tests for lint.run, trace.query, and runtime.trace content gating."""
import json
import sqlite3
import time

from conftest import run

from tools.lint.run import LintRun
from tools.trace.query import TraceQuery


def test_lint_graceful_when_no_linters(project, ctx, monkeypatch):
    # Force "nothing installed" so the test is deterministic across machines.
    monkeypatch.setattr("tools.lint.run.shutil.which", lambda *_a, **_k: None)
    r = run(LintRun().execute({"path": str(project)}, ctx()))
    assert r.status == "ok" and r.result["passed"] is None
    assert "missing" in r.result


def test_lint_runs_installed_tool(project, ctx, monkeypatch):
    # Pretend a linter exists and stub the subprocess to a clean pass.
    monkeypatch.setattr("tools.lint.run.shutil.which", lambda *_a, **_k: "/usr/bin/fake")

    async def fake_run(argv, cwd, timeout):
        return 0, "All checks passed", ""
    monkeypatch.setattr("tools.lint.run._run", fake_run)
    r = run(LintRun().execute({"path": str(project), "linters": ["ruff"]}, ctx()))
    assert r.status == "ok" and r.result["passed"] is True
    assert "ruff" in r.result["ran"]


def _seed_trace(tmp_path):
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
    conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("R1", now - 10, now - 5, "cfi", "do a thing", "done", "ok",
                  None, 1234, 0.0, None))
    conn.execute("INSERT INTO events (run_id, ts, kind, iteration, payload_json)"
                 " VALUES (?,?,?,?,?)", ("R1", now - 9, "tool_call", 1,
                 '{"name": "code.run", "args": "..."}'))
    conn.execute("INSERT INTO events (run_id, ts, kind, iteration, payload_json)"
                 " VALUES (?,?,?,?,?)", ("R1", now - 8, "tool_result", 1,
                 '{"status": "error", "error": "boom"}'))
    conn.commit(); conn.close()
    return str(db)


def test_trace_runs_events_failures(tmp_path, ctx):
    db = _seed_trace(tmp_path)
    cfg = {"tools": {}, "trace": {"db_path": db}}

    r = run(TraceQuery().execute({"view": "runs"}, ctx(config=cfg)))
    assert r.status == "ok" and r.result["count"] == 1
    assert r.result["runs"][0]["run_id"] == "R1"

    r = run(TraceQuery().execute({"view": "events", "run_id": "R1"}, ctx(config=cfg)))
    assert r.status == "ok" and r.result["count"] == 2

    r = run(TraceQuery().execute({"view": "failures"}, ctx(config=cfg)))
    assert r.status == "ok" and r.result["count"] == 1
    assert "boom" in str(r.result["failures"][0]["error"])


def test_trace_events_needs_run_id(tmp_path, ctx):
    db = _seed_trace(tmp_path)
    r = run(TraceQuery().execute({"view": "events"},
                                 ctx(config={"tools": {}, "trace": {"db_path": db}})))
    assert r.status == "error" and "run_id" in r.error


def test_trace_is_readonly(tmp_path, ctx):
    db = _seed_trace(tmp_path)
    # The tool opens mode=ro; prove a write through its connection would fail.
    from tools.trace.query import _connect_ro
    conn = _connect_ro(db)
    try:
        import pytest
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM runs")
    finally:
        conn.close()


# ---- runtime.trace: log_content=false must log just metadata -----------------
def _read_events(db):
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT kind, payload_json FROM events ORDER BY id").fetchall()
    blobs = [p for _, p in rows]
    runs = conn.execute("SELECT user_message, final_answer FROM runs").fetchall()
    conn.close()
    return {k: json.loads(p) for k, p in rows}, blobs, runs


def test_trace_log_content_false_strips_event_payloads(tmp_path):
    from runtime.trace import Trace
    db = str(tmp_path / "trace.db")
    tr = Trace(db, log_content=False)
    tr.start_run("R1", "secret user message", owner="cfi")
    tr.log("R1", "run_start", 0, {"message": "secret user message",
                                  "share_private": False})
    tr.log("R1", "tool_result", 1, {"tool": "fs.read", "args": {"path": "/s"},
                                    "status": "ok", "error": None,
                                    "result_preview": "secret file bytes",
                                    "latency_ms": 4, "tokens": 9,
                                    "private": False})
    tr.log("R1", "verify", 2, {"ok": False, "attempt": 1,
                               "command": "pytest -q",
                               "report": "secret test output"})
    tr.finish_run("R1", "ok", final_answer="secret final answer")
    tr.log("R1", "run_finish", 2, {"status": "ok", "answer": "secret final answer",
                                   "error": None, "budget": {},
                                   "trajectory": "fs.read→ok"})
    tr.close()

    by_kind, blobs, runs = _read_events(db)
    assert runs == [("", "")]                        # runs columns already gated
    for secret in ("secret user message", "secret file bytes",
                   "secret test output", "secret final answer"):
        assert not any(secret in b for b in blobs), secret

    assert by_kind["run_start"]["message"] == "<stripped>"
    assert by_kind["run_start"]["share_private"] is False      # metadata kept
    assert by_kind["tool_result"]["result_preview"] == "<stripped>"
    assert by_kind["tool_result"]["args"] == "<stripped>"
    assert by_kind["tool_result"]["status"] == "ok"            # metadata kept
    assert by_kind["verify"]["report"] == "<stripped>"
    assert by_kind["verify"]["command"] == "pytest -q"         # metadata kept
    assert by_kind["run_finish"]["answer"] == "<stripped>"
    assert by_kind["run_finish"]["trajectory"] == "fs.read→ok"  # metadata kept


def test_trace_log_content_false_strips_all_content_event_kinds(tmp_path):
    # Stripping is by content-bearing KEY, not by event-kind whitelist:
    # model_turn prose/tool-calls, confirmation + question requests, and
    # sub-agent tasks must not land verbatim either.
    from runtime.trace import Trace
    db = str(tmp_path / "trace.db")
    tr = Trace(db, log_content=False)
    tr.start_run("R1", "msg")
    tr.log("R1", "model_turn", 1, {"model": "m", "usage": {},
                                   "tool_calls": [{"name": "fs.read",
                                                   "args": "secret call args"}],
                                   "content": "secret prose", "content_len": 12})
    tr.log("R1", "confirmation_request", 1, {"confirmation_id": "c",
                                             "tool": "job.start",
                                             "args": {"command": "secret command"},
                                             "timeout_s": 300})
    tr.log("R1", "confirmation", 1, {"tool": "job.start", "approved": True,
                                     "via": "web"})
    tr.log("R1", "subagent_start", 1, {"name": "sub", "depth": 1, "model": "m",
                                       "tools": ["fs.read"], "task": "secret task"})
    tr.log("R1", "questions_request", 1, {"ask_id": "a",
                                          "questions": ["secret question?"],
                                          "timeout_s": 600})
    tr.close()

    by_kind, blobs, _ = _read_events(db)
    for secret in ("secret prose", "secret call args", "secret command",
                   "secret task", "secret question"):
        assert not any(secret in b for b in blobs), secret

    assert by_kind["model_turn"]["content"] == "<stripped>"
    assert by_kind["model_turn"]["tool_calls"] == "<stripped>"
    assert by_kind["model_turn"]["model"] == "m"                 # metadata kept
    assert by_kind["confirmation_request"]["args"] == "<stripped>"
    assert by_kind["confirmation_request"]["tool"] == "job.start"
    assert by_kind["subagent_start"]["task"] == "<stripped>"
    assert by_kind["subagent_start"]["name"] == "sub"
    assert by_kind["questions_request"]["questions"] == "<stripped>"
    assert by_kind["questions_request"]["timeout_s"] == 600


def test_trace_log_content_true_keeps_event_payloads(tmp_path):
    from runtime.trace import Trace
    db = str(tmp_path / "trace.db")
    tr = Trace(db, log_content=True)
    tr.start_run("R1", "hello", owner="cfi")
    tr.log("R1", "run_start", 0, {"message": "hello", "share_private": False})
    tr.log("R1", "tool_result", 1, {"tool": "fs.read", "args": {"path": "/s"},
                                    "status": "ok", "result_preview": "bytes"})
    tr.log("R1", "verify", 1, {"ok": True, "report": "all green"})
    tr.finish_run("R1", "ok", final_answer="done")
    tr.log("R1", "run_finish", 1, {"status": "ok", "answer": "done"})
    tr.close()

    by_kind, _, runs = _read_events(db)
    assert runs == [("hello", "done")]
    assert by_kind["run_start"]["message"] == "hello"
    assert by_kind["tool_result"]["result_preview"] == "bytes"
    assert by_kind["verify"]["report"] == "all green"
    assert by_kind["run_finish"]["answer"] == "done"


# ---- trace WAL mode + retention pruning ----

def test_trace_db_runs_in_wal_mode(tmp_path):
    from runtime.trace import Trace
    tr = Trace(str(tmp_path / "trace.db"))
    mode = tr._conn.execute("PRAGMA journal_mode").fetchone()[0]
    tr.close()
    assert mode == "wal"


def test_trace_retention_prunes_old_runs(tmp_path):
    import time as _time

    from runtime.trace import Trace
    db = str(tmp_path / "trace.db")
    tr = Trace(db)
    old_ts = _time.time() - 40 * 86400
    tr._conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES ('old', ?, 'ok')", (old_ts,))
    tr._conn.execute(
        "INSERT INTO events (run_id, ts, kind, payload_json) "
        "VALUES ('old', ?, 'x', '{}')", (old_ts,))
    tr.start_run("new", "hi")
    tr.close()
    # Reopening with retention drops the old run + its events, keeps the new one.
    tr = Trace(db, retention_days=30)
    ids = [r[0] for r in tr._conn.execute("SELECT id FROM runs")]
    ev = tr._conn.execute(
        "SELECT COUNT(*) FROM events WHERE run_id='old'").fetchone()[0]
    tr.close()
    assert ids == ["new"] and ev == 0


def test_trace_retention_zero_keeps_everything(tmp_path):
    import time as _time

    from runtime.trace import Trace
    db = str(tmp_path / "trace.db")
    tr = Trace(db)
    old_ts = _time.time() - 400 * 86400
    tr._conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES ('ancient', ?, 'ok')",
        (old_ts,))
    tr.close()
    tr = Trace(db, retention_days=0)
    ids = [r[0] for r in tr._conn.execute("SELECT id FROM runs")]
    tr.close()
    assert "ancient" in ids
