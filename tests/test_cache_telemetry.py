"""Cache telemetry (docs/harness-lessons.md #4a): every model turn records
its wall-clock timings (turn_s; ttft_s on the streaming path) and the
prefix-cache hit ratio into the model_turn trace event; eval results carry
a tokens-weighted cache_hit_ratio column the benchmark compare aggregates
per variant label."""
import asyncio
import sqlite3

import pytest
from conftest import run
from test_eval_harness import _case, _FakeRuntime, _judge_ok
from test_loop_regressions import _final, _Registry, _runtime

from runtime import eval_runner
from runtime.eval_store import EvalStore


class _RichFakeRuntime(_FakeRuntime):
    """_FakeRuntime with a full token breakdown in the run budget (the
    stock fake reports total-only)."""
    async def run(self, message, **kwargs):
        self.calls.append((message, kwargs))
        answer = self._answers.pop(0) if self._answers else "ok"
        return {"run_id": f"fake-{len(self.calls)}", "status": "ok",
                "answer": answer,
                "trajectory": "web.search(q)→ok",
                "tools_used": ["web.search"],
                "budget": {"iterations": 2, "cost_usd": 0.0,
                           "tokens": {"total": 10, "prompt": 100,
                                      "cached": 60}}}


# ---- model_turn event --------------------------------------------------------

def test_model_turn_event_carries_cache_telemetry():
    """A turn reporting prompt_tokens_details.cached_tokens gets its ratio
    computed at the emit site; the timings ride through from the model
    client (None-safe on paths without them)."""
    rt, _ = _runtime(_Registry([]), [_final("done")])
    base_turn = rt._model_turn

    async def usage_turn(messages, tools_schema, model=None, think=True,
                         sampling=None):
        out = await base_turn(messages, tools_schema, model=model,
                              think=think, sampling=sampling)
        out["usage"] = {"prompt_tokens": 1000, "completion_tokens": 10,
                        "prompt_tokens_details": {"cached_tokens": 700}}
        out["turn_s"] = 2.5
        out["ttft_s"] = 0.4
        return out
    rt._model_turn = usage_turn
    seen = []

    async def cap(ev):
        seen.append(ev)
    out = asyncio.run(rt.run("hi", on_event=cap))
    assert out["status"] == "ok"
    turns = [e for e in seen if e["type"] == "model_turn"]
    assert len(turns) == 1
    assert turns[0]["data"]["cache_hit_ratio"] == 0.7
    assert turns[0]["data"]["turn_s"] == 2.5
    assert turns[0]["data"]["ttft_s"] == 0.4


def test_model_turn_event_no_usage_is_none_safe():
    """The fake-model default (usage {}) yields ratio None, never a crash
    or a division by zero."""
    rt, _ = _runtime(_Registry([]), [_final("done")])
    seen = []

    async def cap(ev):
        seen.append(ev)
    asyncio.run(rt.run("hi", on_event=cap))
    turn = [e for e in seen if e["type"] == "model_turn"][0]
    assert turn["data"]["cache_hit_ratio"] is None
    assert turn["data"]["ttft_s"] is None
    assert turn["data"]["turn_s"] is None


# ---- eval runner: per-case ratio from the run budget --------------------------

def test_run_case_records_cache_hit_ratio(tmp_path, monkeypatch):
    """The case's harness turns sum prompt/cached tokens from each run's
    budget; the row stores the tokens-weighted ratio (judge/driver calls
    excluded). No prompt tokens → NULL, not a crash."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _RichFakeRuntime(["ok"])
    store = EvalStore(tmp_path / "eval.db")
    row = run(eval_runner.run_case(rt, _case(), store))
    assert row["cache_hit_ratio"] == 0.6     # 60 cached of 100 prompt
    assert store.results("demo")[0]["cache_hit_ratio"] == 0.6
    store.close()


def test_run_case_without_prompt_tokens_records_null_ratio(
        tmp_path, monkeypatch):
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["ok"])                # total-only budget, like error paths
    store = EvalStore(tmp_path / "eval.db")
    row = run(eval_runner.run_case(rt, _case(), store))
    assert row["cache_hit_ratio"] is None
    store.close()


# ---- store: column migration + compare aggregation ----------------------------

def test_cache_ratio_migration_and_compare_aggregate(tmp_path):
    db = tmp_path / "eval.db"
    # a pre-telemetry database: opening it must add the column exactly once
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
    for tid, ratio in (("case-a", 0.8), ("case-a", 0.4), ("case-b", None)):
        s.record_result(test_id=tid, passed=True, score=9.0, judge_notes="n",
                        judge_model="m", cost_usd=0.0, tokens=10,
                        elapsed_s=1.0, status="ok", run_ids=[], transcript=[],
                        brain="v1", benchmark=True, cache_hit_ratio=ratio)
    assert s.results("old")[0]["cache_hit_ratio"] is None   # legacy rows
    cmp = s.compare_brains(["v1"])
    a = next(c for c in cmp if c["test_id"] == "case-a")["per_brain"]["v1"]
    assert a["avg_cache_hit_ratio"] == pytest.approx(0.6)  # mean of the two
    b = next(c for c in cmp if c["test_id"] == "case-b")["per_brain"]["v1"]
    assert b["avg_cache_hit_ratio"] is None  # no telemetry rows → None
    s.close()
    s2 = EvalStore(db)                       # reopen: no-op migration
    assert sorted(r["cache_hit_ratio"] for r in s2.results("case-a")) == [0.4, 0.8]
    s2.close()
