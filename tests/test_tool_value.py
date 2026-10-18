"""runtime/tool_value.py + scripts/tool_value.py + the admin endpoint —
per-tool telemetry over a synthetic trace.db."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.tool_value import collect, report  # noqa: E402
from scripts.tool_value import main  # noqa: E402


def _make_db(path) -> None:
    con = sqlite3.connect(path)
    # IF NOT EXISTS: the web app boot may have initialized the DB already.
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,"
                " started_at REAL, finished_at REAL, user_message TEXT,"
                " final_answer TEXT, status TEXT, error TEXT,"
                " summary_json TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY,"
                " run_id TEXT, ts REAL, kind TEXT, iteration INTEGER,"
                " payload_json TEXT)")
    # r1 ok (used fs.read twice, one error), r2 ok (no tools), r3 error (web.fetch)
    for rid, st in (("r1", "ok"), ("r2", "ok"), ("r3", "error")):
        con.execute("INSERT INTO runs (id, started_at, status)"
                    " VALUES (?,0,?)", (rid, st))
    ev = [("r1", '{"tool":"fs.read","status":"ok"}'),
          ("r1", '{"tool":"fs.read","status":"error","error":"nope"}'),
          ("r3", '{"tool":"web.fetch","status":"error","error":"403"}')]
    con.executemany("INSERT INTO events (run_id,ts,kind,payload_json)"
                    " VALUES (?,0,'tool_result',?)", ev)
    con.commit()
    con.close()


def _db(tmp_path) -> str:
    p = tmp_path / "trace.db"
    _make_db(p)
    return str(p)


def test_collect_and_report(tmp_path):
    tools, runs = collect(_db(tmp_path))
    assert tools["fs.read"]["calls"] == 2
    assert tools["fs.read"]["errors"] == 1
    out = report(tools, runs, min_calls=1, by_errors=False)
    assert "fs.read" in out and "web.fetch" in out
    # fs.read: used in 1/3 runs, that run ok → 100%; without: r2 ok, r3 error → 50%
    line = next(l for l in out.splitlines() if l.startswith("fs.read"))
    assert "100%" in line and "50%" in line
    # web.fetch: its only run errored → ok_with 0%
    line = next(l for l in out.splitlines() if l.startswith("web.fetch"))
    assert "0%" in line


def test_tail_section(tmp_path):
    tools, runs = collect(_db(tmp_path))
    out = report(tools, runs, min_calls=5, by_errors=False)
    assert "rarely used" in out and "fs.read (2)" in out


def test_missing_db_is_graceful(tmp_path, capsys):
    sys.argv = ["tool-value.py", "--db", str(tmp_path / "nope.db")]
    assert main() == 0
    assert "nothing to measure" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_tool_value_endpoint(web_app, web_client):
    """Eval's Tool telemetry button: the report over the
    configured trace DB, read-only, off the event loop."""
    app = web_app()
    _make_db(app.state.runtime.config["trace"]["db_path"])
    async with web_client(app) as c:
        r = await c.get("/api/admin/evals/tool-value?min_calls=1")
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] and "fs.read" in j["text"] and "web.fetch" in j["text"]


def test_case_strengths_registry_match():
    """A case tag that IS a registered strength counts directly (the registry
    is admin-editable — a new tag like bugtrace measures without a code
    change); the alias table and strength: pins still apply."""
    from runtime.eval_strengths import case_strengths
    reg = {"bugtrace": "…", "coding": "…"}
    assert case_strengths(["bugtrace"], reg) == {"bugtrace"}
    assert case_strengths(["tb"], reg) == {"coding"}            # alias table
    assert (case_strengths(["strength:vision", "tb"], reg)
            == {"vision", "coding"})                            # pin + alias
    assert case_strengths(["bugtrace"]) == set()                # no registry
    assert case_strengths(["random-thing"], reg) == set()


def test_current_labels_normalizes():
    """Benchmark show-archived: labels map to current presets by name, alias
    or served_id, dash/underscore-insensitive (results record served_ids)."""
    from web.routes_eval import _current_labels
    cfg = {"models": {"presets": {
        "occamy-1-0": {"alias": "local-orchestrator",
                       "served_id": "occamy-mini"},
        " turbo": {"alias": None}}}}
    out = _current_labels(cfg)
    assert {"occamy10", "localorchestrator", "occamymini",
            "turbo"} <= set(out)
    assert _current_labels({}) == []


@pytest.mark.asyncio
async def test_benchmark_brains_carries_current_labels(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        j = (await c.get("/api/admin/evals/benchmark/brains")).json()
        assert "current_labels" in j
        assert "localorchestrator" in j["current_labels"]
