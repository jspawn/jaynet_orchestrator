"""Eval statistics: brain column, EvalStore aggregates (kpis / per-case /
series / compare) at controlled timestamps, and the runner's brain recording.

Timestamps are set by recording a row and then UPDATEing ts directly — the
simplest way to place runs inside/outside aggregation windows.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

from conftest import run
from test_eval_harness import _case, _FakeRuntime, _judge_ok

from runtime import eval_runner
from runtime.eval_store import EvalStore

_NOW = time.time()
_DAY = 86400


def _rec(s: EvalStore, test_id: str, ts: float, passed: bool, *,
         score: float | None = 8.0, judge_model: str = "cloud-judge",
         cost: float = 0.01, elapsed: float = 1.0, status: str = "ok",
         brain: str | None = None, benchmark: bool = False) -> dict:
    row = s.record_result(test_id=test_id, passed=passed, score=score,
                          judge_notes="n", judge_model=judge_model,
                          cost_usd=cost, tokens=10, elapsed_s=elapsed,
                          status=status, run_ids=[], transcript=[], brain=brain,
                          benchmark=benchmark)
    with s._lock, s._conn:
        s._conn.execute("UPDATE results SET ts=? WHERE id=?", (ts, row["id"]))
    return row


def _day_ts(y, m, d, hour=12) -> float:
    return datetime(y, m, d, hour).timestamp()   # local time, like series()


# ---- brain column -----------------------------------------------------------

def test_brain_column_and_legacy_migration(tmp_path):
    db = tmp_path / "eval.db"
    # a pre-brain database: opening it must add the column exactly once
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, test_id TEXT NOT NULL,
            ts REAL NOT NULL, passed INTEGER NOT NULL, score REAL,
            judge_notes TEXT, judge_model TEXT, cost_usd REAL DEFAULT 0,
            tokens INTEGER DEFAULT 0, elapsed_s REAL DEFAULT 0, status TEXT,
            run_ids TEXT, transcript TEXT);
        INSERT INTO results (test_id, ts, passed) VALUES ('old', 1.0, 1);
    """)
    conn.close()
    s = EvalStore(db)
    row = s.record_result(test_id="t", passed=True, score=9.0, judge_notes="n",
                          judge_model="m", cost_usd=0.0, tokens=0,
                          elapsed_s=0.0, status="ok", run_ids=[],
                          transcript=[], brain="brain-a")
    assert row["brain"] == "brain-a"
    assert s.results("old")[0]["brain"] is None      # legacy rows stay NULL
    assert s.record_result(test_id="t2", passed=True, score=None,
                           judge_notes="n", judge_model="m", cost_usd=0.0,
                           tokens=0, elapsed_s=0.0, status="ok", run_ids=[],
                           transcript=[])["brain"] is None   # default
    s.close()
    s2 = EvalStore(db)                               # reopen: no-op migration
    assert s2.results("t")[0]["brain"] == "brain-a"
    s2.close()


# ---- benchmark flag: the default statistics view excludes variant reps ------

def test_benchmark_rows_excluded_by_default_selectable_by_label(tmp_path):
    s = EvalStore(tmp_path / "eval.db")
    _rec(s, "t", _NOW, True, brain="local-orchestrator")              # live run
    _rec(s, "t", _NOW, False, brain="v1-t0", benchmark=True)          # rep 1
    _rec(s, "t", _NOW, True, brain="v1-t0", benchmark=True)           # rep 2
    # default view: live rows only — the benchmark reps move nothing
    k = s.kpis()
    assert k["runs"] == 1 and k["pass_rate"] == 1.0
    pcs = s.per_case_stats()
    assert pcs[0]["runs"] == 1 and pcs[0]["flakiness"] == 0.0
    assert s.series()[0]["runs"] == 1
    tr = s.trend("t")
    assert len(tr) == 1 and tr[0]["passed"] == 1
    # scoped to the variant label: exactly its rows
    k = s.kpis(brain="v1-t0")
    assert k["runs"] == 2 and k["pass_rate"] == 0.5
    pcs = s.per_case_stats(brain="v1-t0")
    assert pcs[0]["runs"] == 2 and pcs[0]["flakiness"] == 1.0
    assert s.series(brain="v1-t0")[0]["runs"] == 2
    assert len(s.trend("t", brain="v1-t0")) == 2
    s.close()


def test_legacy_rows_default_to_live_view(tmp_path):
    # Rows recorded before the benchmark column existed migrate to 0 —
    # they stay in the default live view.
    s = EvalStore(tmp_path / "eval.db")
    row = _rec(s, "t", _NOW, True, brain="v1-t0")      # no benchmark kw
    assert row["benchmark"] == 0
    assert s.kpis()["runs"] == 1
    s.close()


def test_run_case_variant_records_benchmark_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["hello", "hello"])
    store = EvalStore(tmp_path / "eval.db")
    run(eval_runner.run_case(rt, _case(), store,
                             variant={"label": "v1", "model": None,
                                      "sampling": None}))
    row = store.results("demo")[0]
    assert row["brain"] == "v1" and row["benchmark"] == 1
    run(eval_runner.run_case(rt, _case(), store))      # plain run: live view
    assert store.results("demo")[0]["benchmark"] == 0
    store.close()


# ---- kpis --------------------------------------------------------------------

def test_kpis_window_fallbacks_and_crashes(tmp_path):
    s = EvalStore(tmp_path / "eval.db")
    _rec(s, "a", _NOW - 10 * _DAY, True, score=8.0, cost=0.02, elapsed=2.0)
    _rec(s, "a", _NOW - _DAY, False, score=None, cost=0.01, elapsed=4.0,
         status="crash", judge_model="local-specialist")
    _rec(s, "b", _NOW - _DAY, True, score=6.0, cost=0.03, elapsed=6.0,
         judge_model="local-qwen")

    k = s.kpis()                                     # all time
    assert k["runs"] == 3 and k["passed"] == 2
    assert abs(k["pass_rate"] - 2 / 3) < 1e-9
    assert k["avg_score"] == 7.0                     # NULL score excluded
    assert abs(k["total_cost_usd"] - 0.06) < 1e-9
    assert abs(k["cost_per_pass"] - 0.03) < 1e-9
    assert k["avg_elapsed_s"] == 4.0
    assert k["judge_fallbacks"] == 2                 # judge_model LIKE local-%
    assert k["crashes"] == 1                         # status != 'ok'

    k = s.kpis(_NOW - 2 * _DAY)                      # window excludes the old row
    assert k["runs"] == 2 and k["passed"] == 1
    assert k["avg_score"] == 6.0
    assert abs(k["cost_per_pass"] - 0.04) < 1e-9
    assert k["judge_fallbacks"] == 2 and k["crashes"] == 1

    k = s.kpis(_NOW + _DAY)                          # empty window, no /0
    assert k["runs"] == 0 and k["pass_rate"] is None
    assert k["avg_score"] is None and k["cost_per_pass"] is None
    assert k["total_cost_usd"] == 0.0
    s.close()


# ---- per-case stats ------------------------------------------------------------

def test_per_case_stats_and_flakiness(tmp_path):
    s = EvalStore(tmp_path / "eval.db")
    for i, ok in enumerate([True, False, True, False]):   # 3 transitions / 3
        _rec(s, "flake", _NOW - (4 - i) * 100, ok,
             score=5.0 + i, brain="b2" if i % 2 else "b1")
    _rec(s, "steady", _NOW - 260, True, score=9.0)        # 1 run → flakiness 0

    stats = {c["test_id"]: c for c in s.per_case_stats()}
    f = stats["flake"]
    assert f["runs"] == 4 and f["pass_rate"] == 0.5
    assert f["avg_score"] == 6.5
    assert f["flakiness"] == 1.0
    assert f["last_passed"] is False
    assert f["last_ts"] == _NOW - 100
    assert f["brains"] == ["b1", "b2"]
    assert abs(f["avg_cost_usd"] - 0.01) < 1e-9
    assert stats["steady"]["flakiness"] == 0.0
    assert stats["steady"]["last_passed"] is True

    # window that keeps only the two most recent flake runs (fail after pass)
    stats = {c["test_id"]: c for c in s.per_case_stats(_NOW - 250)}
    assert stats["flake"]["runs"] == 2
    assert stats["flake"]["flakiness"] == 1.0
    assert "steady" not in stats
    s.close()


# ---- daily series --------------------------------------------------------------

def test_series_buckets_by_local_day(tmp_path):
    s = EvalStore(tmp_path / "eval.db")
    d1, d2 = _day_ts(2026, 1, 10), _day_ts(2026, 1, 12)
    _rec(s, "a", d2, True, score=8.0)                # inserted out of order
    _rec(s, "a", d1, True, score=6.0)
    _rec(s, "b", d1, False, score=4.0)
    days = s.series()
    assert [d["day"] for d in days] == ["2026-01-10", "2026-01-12"]  # oldest first
    assert days[0]["runs"] == 2 and days[0]["pass_rate"] == 0.5
    assert days[0]["avg_score"] == 5.0
    assert days[1] == {"day": "2026-01-12", "runs": 1, "pass_rate": 1.0,
                       "avg_score": 8.0}
    assert [d["day"] for d in s.series(d1 + _DAY)] == ["2026-01-12"]
    s.close()


# ---- window comparison ------------------------------------------------------------

def test_compare_windows_and_empty_sides(tmp_path):
    s = EvalStore(tmp_path / "eval.db")
    a0, a1 = _NOW - 20 * _DAY, _NOW - 10 * _DAY      # window A
    b0, b1 = _NOW - 5 * _DAY, _NOW                   # window B
    _rec(s, "both", _NOW - 15 * _DAY, True, score=8.0)
    _rec(s, "both", _NOW - 2 * _DAY, False, score=4.0)
    _rec(s, "only-a", _NOW - 15 * _DAY, True, score=9.0)
    _rec(s, "only-b", _NOW - 2 * _DAY, True, score=7.0)

    cmp = {c["test_id"]: c for c in s.compare(a0, a1, b0, b1)}
    both = cmp["both"]
    assert both["a_runs"] == 1 and both["b_runs"] == 1
    assert both["a_pass_rate"] == 1.0 and both["b_pass_rate"] == 0.0
    assert both["pass_delta"] == -1.0                # b − a
    assert both["score_delta"] == -4.0
    only_a = cmp["only-a"]
    assert only_a["b_runs"] == 0 and only_a["b_pass_rate"] is None
    assert only_a["pass_delta"] is None and only_a["score_delta"] is None
    only_b = cmp["only-b"]
    assert only_b["a_runs"] == 0 and only_b["pass_delta"] is None
    assert set(cmp) == {"both", "only-a", "only-b"}
    # windows with no rows at all: empty, not an error
    assert s.compare(1.0, 2.0, 3.0, 4.0) == []
    s.close()


# ---- runner records the brain -------------------------------------------------------

def test_run_case_records_brain(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["hello"])
    store = EvalStore(tmp_path / "eval.db")
    run(eval_runner.run_case(rt, _case(), store))
    assert store.results("demo")[0]["brain"] == "fake-brain"
    # a runtime without a .model attribute still records, brain NULL
    del rt.model
    run(eval_runner.run_case(rt, _case(), store))
    assert store.results("demo")[0]["brain"] is None
    store.close()
