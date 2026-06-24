"""Tests for lint.run and trace.query."""
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
