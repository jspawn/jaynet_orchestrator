"""Budget token accounting: cached prompt tokens count at reduced weight.

Local-first rationale: llama.cpp serves cached prefix tokens from the KV
cache (near-free), and in long tool loops >90% of prompt tokens are cache
hits — full-weight counting killed healthy runs on re-transmission rather
than real work. Raw counters stay untouched for accounting; only the
ceiling/pressure view (total_tokens) is weighted.
"""
import pytest

from runtime.budget import Budget, BudgetExceeded


def _b(weight=0.1, **kw):
    d = dict(max_iterations=100, max_wall_clock_s=0, max_cost_usd=10.0,
             max_total_tokens=1000, cached_token_weight=weight)
    d.update(kw)
    return Budget(**d)


def test_cached_tokens_downweighted():
    b = _b()
    b.add_usage("m", prompt=1000, completion=100, cached=900)
    # 100 uncached + 900 * 0.1 + 100 completion
    assert b.total_tokens == 290


def test_raw_counters_preserved():
    b = _b()
    b.add_usage("m", prompt=1000, completion=100, cached=900)
    s = b.summary()["tokens"]
    assert s["prompt"] == 1000 and s["cached"] == 900
    assert s["completion"] == 100 and s["total"] == b.total_tokens


def test_ceiling_uses_effective_tokens():
    # A long tool loop: 831k cumulative prompt of which 796k are cache hits.
    # Raw counting (836k) would blow a 130k ceiling; effective is ~120k.
    b = _b(max_total_tokens=130_000)
    b.add_usage("m", prompt=831_000, completion=5_000, cached=796_000)
    b.check()
    assert b.tokens_prompt + b.tokens_completion > b.max_total_tokens  # raw WOULD trip


def test_ceiling_still_trips_on_real_growth():
    b = _b(max_total_tokens=1000)
    b.add_usage("m", prompt=900, completion=200, cached=0)
    with pytest.raises(BudgetExceeded):
        b.check()


def test_weight_one_restores_full_counting():
    b = _b(weight=1.0)
    b.add_usage("m", prompt=1000, completion=100, cached=900)
    assert b.total_tokens == 1100


def test_weight_zero_makes_cache_hits_free():
    b = _b(weight=0.0)
    b.add_usage("m", prompt=1000, completion=100, cached=900)
    assert b.total_tokens == 200


def test_cached_never_exceeds_prompt():
    # defensive: a backend reporting more cached than prompt clamps at 0
    b = _b()
    b.add_usage("m", prompt=100, completion=10, cached=500)
    assert b.total_tokens == int(500 * 0.1 + 10)


def test_pressure_uses_effective_tokens():
    b = _b(max_total_tokens=1000)
    b.add_usage("m", prompt=1000, completion=0, cached=900)
    frac, name = b.pressure()
    assert name == "token" and abs(frac - 0.19) < 1e-9


def test_zero_means_no_ceiling_everywhere():
    # 0 = unlimited for ALL ceilings (the admin budget editor's "off" state),
    # not just the wall clock — check() must never raise on a zeroed budget.
    b = Budget(max_iterations=0, max_wall_clock_s=0, max_cost_usd=0,
               max_total_tokens=0)
    b.iterations = 500
    b.cost_usd = 42.0
    b.add_usage("m", prompt=10**7, completion=10**6, cached=0)
    b.check()          # must not raise
    frac, name = b.pressure()
    assert frac == 0.0
