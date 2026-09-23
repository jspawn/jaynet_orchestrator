"""Eval statistics: brain column, EvalStore aggregates (kpis / per-case /
series / compare) at controlled timestamps, and the runner's brain recording.

Timestamps are set by recording a row and then UPDATEing ts directly — the
simplest way to place runs inside/outside aggregation windows.
"""
from __future__ import annotations

import json
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



def test_stable_passes(tmp_path):
    """Stable = last 3 non-benchmark results all pass (3+ must exist).
    Flaky cases, short histories and benchmark-only streaks don't qualify."""
    s = EvalStore(tmp_path / "eval.db")
    t = _NOW
    for i in range(3):                       # stable: 3/3 pass
        _rec(s, "stable-case", t + i, True)
    for i, p in enumerate([True, True, False, True]):   # flaky: 1 fail in 4
        _rec(s, "flaky-case", t + i, p)
    for i in range(2):                       # too little history
        _rec(s, "young-case", t + i, True)
    for i in range(4):                       # benchmark rep spam != stable
        _rec(s, "bench-case", t + i, True, benchmark=True)
    for i, p in enumerate([True, True, True, True, False]):  # fail after streak
        _rec(s, "was-stable", t + i, p)
    assert s.stable_passes() == {"stable-case"}
    s.close()


def test_stable_passes_scoped_by_brain(tmp_path):
    """Stability is per-brain: a case stable under brain A is NOT stable when
    the current brain is B (a swap invalidates inherited streaks). Unlabeled
    rows (legacy/manual) count toward any brain."""
    s = EvalStore(tmp_path / "eval.db")
    t = _NOW
    for i in range(3):
        _rec(s, "case-a", t + i, True, brain="spark")      # stable under spark
        _rec(s, "case-b", t + i, True, brain="gemma")      # stable under gemma
        _rec(s, "case-c", t + i, True)                     # unlabeled: any brain
    # mixed history under the current brain: last 3 spark rows are not all
    # passes even though the gemma rows in between passed
    for i, (b, p) in enumerate([("spark", True), ("gemma", True),
                                ("spark", True), ("spark", False)]):
        _rec(s, "case-d", t + 10 + i, p, brain=b)
    assert s.stable_passes(brain="spark") == {"case-a", "case-c"}
    assert s.stable_passes(brain="gemma") == {"case-b", "case-c"}
    assert s.stable_passes(brain="k2") == {"case-c"}       # fresh brain
    assert s.stable_passes() == {"case-a", "case-b", "case-c"}  # unscoped: d flaky
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


# ---- version column + scheduled suites ----------------------------------------

def test_version_recorded_and_listed(tmp_path):
    import runtime
    s = EvalStore(tmp_path / "eval.db")
    row = _rec(s, "t", _NOW, True)
    assert row["version"] == runtime.__version__
    assert s.versions() == [runtime.__version__]
    s.close()


def test_schedules_crud_due_and_fire_stamp(tmp_path):
    s = EvalStore(tmp_path / "eval.db")
    row = s.add_schedule(selector="tag:web", every_s=3600)
    assert row["enabled"] == 1 and row["last_fired"] is None
    assert [r["id"] for r in s.schedules()] == [row["id"]]
    # never-fired = due
    assert [r["id"] for r in s.due_schedules()] == [row["id"]]
    s.mark_schedule_fired(row["id"])
    assert s.due_schedules() == []
    # … and due again once the interval elapses
    assert [r["id"] for r in s.due_schedules(time.time() + 3601)] == [row["id"]]
    # disabled schedules never fire; toggle back on works
    assert s.set_schedule_enabled(row["id"], False)["enabled"] == 0
    assert s.due_schedules(time.time() + 99999) == []
    assert s.set_schedule_enabled(row["id"], True)["enabled"] == 1
    assert s.delete_schedule(row["id"]) is True
    assert s.delete_schedule(row["id"]) is False
    assert s.set_schedule_enabled("nope", True) is None
    s.close()


# ---- strength matrix ---------------------------------------------------------

def test_strength_matrix_aggregates_per_brain_and_strength(tmp_path):
    """Each result counts under EVERY strength its case exercises; live rows
    and benchmark reps both feed the matrix (the brain label is the measured
    model). Unmapped cases feed nothing."""
    s = EvalStore(tmp_path / "eval.db")
    strengths = {"tb-x": {"coding"}, "gaia-y": {"research", "reasoning"},
                 "unmapped-z": set()}
    _rec(s, "tb-x", _NOW, True, brain="qwen-dense")
    _rec(s, "tb-x", _NOW, False, brain="qwen-dense")
    _rec(s, "tb-x", _NOW, True, brain="k2-moe")
    _rec(s, "gaia-y", _NOW, True, brain="k2-moe")
    _rec(s, "gaia-y", _NOW, False, brain="qwen-dense")
    _rec(s, "unmapped-z", _NOW, True, brain="k2-moe")
    _rec(s, "tb-x", _NOW, False)                        # no brain label: skip
    cells = s.strength_matrix(case_strengths=strengths.get)
    by = {(c["strength"], c["brain"]): c for c in cells}
    assert by[("coding", "qwen-dense")]["runs"] == 2
    assert by[("coding", "qwen-dense")]["pass_rate"] == 0.5
    assert by[("coding", "k2-moe")]["pass_rate"] == 1.0
    assert by[("research", "k2-moe")]["runs"] == 1
    # gaia-y counts under reasoning too
    assert by[("reasoning", "qwen-dense")]["pass_rate"] == 0.0
    # unmapped case and brain-less rows vanish
    assert all(c["brain"] for c in cells)
    assert len(cells) == 6  # nothing extra beyond the mapped pairs above
    s.close()


def test_strength_matrix_since_window(tmp_path):
    s = EvalStore(tmp_path / "eval.db")
    strengths = {"tb-x": {"coding"}}
    _rec(s, "tb-x", _NOW - 10 * 86400, True, brain="old")
    _rec(s, "tb-x", _NOW, False, brain="new")
    cells = s.strength_matrix(_NOW - 86400, case_strengths=strengths.get)
    assert len(cells) == 1 and cells[0]["brain"] == "new"
    s.close()


# ---- fallback + provenance columns (code-audit P1) -----------------------------

_PROV = dict(fallback="local-specialist->local-orchestrator",
             git_sha="abc123", git_dirty=1, prompt_hash="p" * 16,
             config_hash="c" * 16, specialist_preset="coder-32b",
             model_files=["Qwen3-4B-Q4_K_M.gguf", "mmproj-f16.gguf"])


def test_fallback_provenance_columns_migration_and_roundtrip(tmp_path):
    db = tmp_path / "eval.db"
    # a pre-provenance database: opening it must add the columns exactly once
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
                          elapsed_s=0.0, status="ok", run_ids=[], transcript=[],
                          **_PROV)
    assert row["fallback"] == _PROV["fallback"]
    assert row["git_sha"] == "abc123" and row["git_dirty"] == 1
    assert row["prompt_hash"] == "p" * 16 and row["config_hash"] == "c" * 16
    assert row["specialist_preset"] == "coder-32b"
    assert json.loads(row["model_files"]) == _PROV["model_files"]
    old = s.results("old")[0]                        # legacy rows stay NULL
    assert old["fallback"] is None and old["git_sha"] is None
    assert old["git_dirty"] is None and old["model_files"] is None
    plain = s.record_result(test_id="t2", passed=True, score=None,
                            judge_notes="n", judge_model="m", cost_usd=0.0,
                            tokens=0, elapsed_s=0.0, status="ok", run_ids=[],
                            transcript=[])
    assert plain["fallback"] is None and plain["git_sha"] is None  # defaults
    s.close()
    s2 = EvalStore(db)                               # reopen: no-op migration
    assert s2.results("t")[0]["specialist_preset"] == "coder-32b"
    s2.close()


def test_run_case_tags_fallback_and_records_provenance(tmp_path, monkeypatch):
    """A served-model mismatch logged during the case window (specialist
    down, brain silently serving) tags the row: judge_notes prefix +
    fallback column. Entries from BEFORE the case don't count."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["hello"])
    rt._served_model_log = [
        {"ts": time.time() - 3600, "requested": "local-specialist",
         "served": "local-orchestrator"},            # stale: outside window
    ]
    orig_run = rt.run

    async def run_with_fallback(message, **kw):
        # mid-case sighting, as the real model client would log it
        rt._served_model_log.append(
            {"ts": time.time(), "requested": "local-specialist",
             "served": "local-orchestrator"})
        return await orig_run(message, **kw)
    rt.run = run_with_fallback
    store = EvalStore(tmp_path / "eval.db")
    run(eval_runner.run_case(rt, _case(), store))
    row = store.results("demo")[0]
    assert row["fallback"] == "local-specialist->local-orchestrator"
    assert row["judge_notes"].startswith(
        "[fallback: requested local-specialist served local-orchestrator]")
    # provenance columns are always populated best-effort (never raise)
    assert "git_sha" in row and "git_dirty" in row
    assert row["git_sha"] is None or len(row["git_sha"]) == 40
    assert row["git_dirty"] in (None, 0, 1)
    assert isinstance(row["config_hash"], str) and len(row["config_hash"]) == 16
    store.close()


def test_run_case_no_fallback_no_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["hello"])                     # no ledger at all
    store = EvalStore(tmp_path / "eval.db")
    run(eval_runner.run_case(rt, _case(), store))
    row = store.results("demo")[0]
    assert row["fallback"] is None
    assert not row["judge_notes"].startswith("[fallback:")
    store.close()


def test_fallbacks_since_window_and_dedupe():
    class _RT:
        pass
    rt = _RT()
    now = time.time()
    rt._served_model_log = [
        {"ts": now - 100, "requested": "a", "served": "b"},   # before window
        {"ts": now, "requested": "x", "served": "y"},
        {"ts": now, "requested": "x", "served": "y"},         # dup
        {"ts": now + 1, "requested": "p", "served": "q"},
    ]
    assert eval_runner._fallbacks_since(rt, now - 1) == [("x", "y"), ("p", "q")]
    assert eval_runner._fallbacks_since(object(), 0) == []    # no ledger


# ---- wilson / mcnemar (runtime.eval_stats, used by scripts/eval-peek.py) ------

def test_wilson_interval_known_values():
    from runtime.eval_stats import wilson_interval
    assert wilson_interval(0, 0) is None
    lo, hi = wilson_interval(18, 32)          # bakeoff's Spark column
    assert abs(lo - 0.394) < 0.01 and abs(hi - 0.718) < 0.01
    lo, hi = wilson_interval(0, 10)           # stays inside [0, 1]
    assert lo == 0.0 and 0.2 < hi < 0.35
    lo, hi = wilson_interval(10, 10)
    assert hi == 1.0 and 0.65 < lo < 0.8


def test_mcnemar_exact_known_values():
    from runtime.eval_stats import mcnemar_exact
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(5, 5) == 1.0                    # no asymmetry at all
    # b=1, c=9: tail = (C(10,0)+C(10,1)) / 2^10, doubled
    assert abs(mcnemar_exact(1, 9) - 22 / 1024) < 1e-12
    assert mcnemar_exact(1, 9) == mcnemar_exact(9, 1)    # symmetric
    assert mcnemar_exact(0, 10) < 0.01                   # 10/10 one way
