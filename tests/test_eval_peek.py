"""scripts/eval-peek.py guard telemetry (audit Tier 2 #6): per-case guards
fired (name->count) plus the pass-after-fire vs pass-while-quiet aggregate,
and graceful degradation when trace.db or the run_ids linkage is missing.

The script is loaded by path (hyphenated name) and driven through main()
with DATA pointed at tmp sqlite fixtures; run_status is stubbed — without a
live admin API it returns an error dict and main() exits before the report.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "eval_peek", ROOT / "scripts/eval-peek.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


peek = _load()

_NOW = time.time()


def _mk_eval_db(path, rows):
    """rows: (test_id, passed, run_ids, status)."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, test_id TEXT NOT NULL,
            ts REAL NOT NULL, passed INTEGER NOT NULL, score REAL,
            judge_notes TEXT, judge_model TEXT, cost_usd REAL DEFAULT 0,
            tokens INTEGER DEFAULT 0, elapsed_s REAL DEFAULT 0, status TEXT,
            run_ids TEXT, transcript TEXT, brain TEXT,
            benchmark INTEGER DEFAULT 0);
    """)
    for i, (tid, passed, rids, status) in enumerate(rows):
        conn.execute(
            "INSERT INTO results (test_id, ts, passed, elapsed_s, judge_notes,"
            " status, run_ids) VALUES (?,?,?,?,?,?,?)",
            (tid, _NOW - 100 + i, passed, 5.0, "note", status,
             json.dumps(rids)))
    conn.commit()
    conn.close()


def _mk_trace_db(path, events):
    """events: (run_id, kind, payload)."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE runs (id TEXT PRIMARY KEY, started_at REAL NOT NULL,
                           finished_at REAL, user_message TEXT,
                           final_answer TEXT, status TEXT, error TEXT,
                           summary_json TEXT);
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            ts REAL NOT NULL, kind TEXT NOT NULL, iteration INTEGER,
            payload_json TEXT,
            FOREIGN KEY (run_id) REFERENCES runs(id));
    """)
    for rid, kind, payload in events:
        conn.execute(
            "INSERT INTO runs (id, started_at) VALUES (?, ?)"
            " ON CONFLICT(id) DO NOTHING", (rid, _NOW - 200))
        conn.execute(
            "INSERT INTO events (run_id, ts, kind, payload_json)"
            " VALUES (?,?,?,?)", (rid, _NOW - 150, kind, json.dumps(payload)))
    conn.commit()
    conn.close()


def _peek(tmp_path, monkeypatch, capsys, *argv) -> str:
    monkeypatch.setattr(peek, "DATA", str(tmp_path))
    monkeypatch.setattr(peek, "run_status",
                        lambda: {"running": False, "current": None,
                                 "last": None})
    monkeypatch.setattr(sys, "argv", ["eval-peek.py", "--since", "0", *argv])
    peek.main()
    return capsys.readouterr().out


def test_guard_fire_rate_and_pass_after_fire(tmp_path, monkeypatch, capsys):
    _mk_eval_db(tmp_path / "eval.db", [
        ("case-a", 1, ["r1"], "ok"),      # fired twice, passed (rescue)
        ("case-b", 0, ["r2"], "ok"),      # quiet, failed
        ("case-c", 1, ["r3"], "ok"),      # fired once, passed
    ])
    _mk_trace_db(tmp_path / "trace.db", [
        ("r1", "guard_fired", {"name": "loop_guard", "phase": "turn", "turn": 3}),
        ("r1", "guard_fired", {"name": "loop_guard", "phase": "turn", "turn": 5}),
        ("r3", "guard_fired", {"name": "loop_guard", "phase": "turn", "turn": 2}),
        ("r1", "tool_call", {"tool": "fs.read"}),
    ])
    out = _peek(tmp_path, monkeypatch, capsys)
    # per case: guards fired with counts, next to the pass/fail verdict
    assert "PASS  case-a" in out and "guards: loop_guard×2" in out
    assert "fail  case-b" in out and "guards:" not in next(
        ln for ln in out.splitlines() if "case-b" in ln)
    assert "guards: loop_guard×1" in out
    # aggregate: total fires, cases involved, pass rate fired vs quiet
    line = next(ln for ln in out.splitlines() if "loop_guard" in ln
                and "fires" in ln)
    assert "3 fires" in line and "2/3 cases" in line
    assert "fired-pass 100%" in line       # both fired cases passed
    assert "quiet-pass   0%" in line       # the one quiet case failed


def test_multiple_guards_sorted_by_cases(tmp_path, monkeypatch, capsys):
    _mk_eval_db(tmp_path / "eval.db", [
        ("case-a", 1, ["r1"], "ok"),
        ("case-b", 0, ["r2"], "ok"),
    ])
    _mk_trace_db(tmp_path / "trace.db", [
        ("r1", "guard_fired", {"name": "budget_guard"}),
        ("r1", "guard_fired", {"name": "loop_guard"}),
        ("r2", "guard_fired", {"name": "loop_guard"}),
    ])
    out = _peek(tmp_path, monkeypatch, capsys)
    assert "guards: budget_guard×1,loop_guard×1" in out
    lines = [ln for ln in out.splitlines()
             if ln.startswith("  ") and "fires" in ln]   # skip the header
    assert "loop_guard" in lines[0]        # more cases first
    assert "budget_guard" in lines[1]


def test_delegation_fanout_and_llm_subcalls_are_distinct(tmp_path, monkeypatch, capsys):
    _mk_eval_db(tmp_path / "eval.db", [
        ("specialist", 0, ["r1"], "ok"),
        ("fanout", 0, ["r2"], "ok"),
        ("vision", 0, ["r3"], "ok"),
    ])
    _mk_trace_db(tmp_path / "trace.db", [
        ("r1", "tool_result", {"tool": "specialist.delegate", "status": "ok",
                               "model": "local-specialist"}),
        ("r2", "tool_result", {"tool": "agent.spawn", "status": "ok"}),
        ("r3", "tool_result", {"tool": "llm.call", "status": "ok",
                               "model": "local-specialist"}),
    ])
    out = _peek(tmp_path, monkeypatch, capsys)
    assert "specialist  " in out and "-> specialist" in out
    assert "fanout  " in out and "-> fanout" in out
    assert "vision  " in out and "-> llm.call" in out
    assert "delegation 1/3 | fanout 1 | llm.call 1" in out


def test_missing_trace_db_notes_no_trace_data(tmp_path, monkeypatch, capsys):
    _mk_eval_db(tmp_path / "eval.db", [("case-a", 1, ["r1"], "ok")])
    out = _peek(tmp_path, monkeypatch, capsys)   # no trace.db at all
    assert "no trace data" in out
    assert "PASS  case-a" in out           # the report itself still renders
    assert "total 1" in out


def test_unlinked_run_ids_and_no_fires(tmp_path, monkeypatch, capsys):
    _mk_eval_db(tmp_path / "eval.db", [
        ("case-a", 1, ["ghost-run"], "ok"),   # run_ids link to nothing
        ("case-b", 1, [], "ok"),              # no run_ids recorded
    ])
    _mk_trace_db(tmp_path / "trace.db", [])
    out = _peek(tmp_path, monkeypatch, capsys)
    assert "no guard fires in window" in out
    assert "total 2" in out
