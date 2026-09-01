"""Wall-clock liveness extensions (Budget): an expiring deadline extends by
grace_s while the run keeps cycling (each tick is a ping), up to
max_extensions — then BudgetExceeded as before. Default stays absolute."""
import time

import pytest

from runtime.budget import Budget, BudgetExceeded


def _b(grace=0, ext=0, wall=1200.0, age=0.0):
    return Budget(max_iterations=0, max_wall_clock_s=wall, max_cost_usd=0,
                  max_total_tokens=0, wall_clock_grace_s=grace,
                  wall_clock_max_extensions=ext,
                  started_at=time.monotonic() - age)


def test_disabled_by_default_raises_immediately():
    with pytest.raises(BudgetExceeded) as ei:
        _b(age=1300).check()
    assert ei.value.reason == "max_wall_clock_s"
    assert ei.value.details["extensions_used"] == 0


def test_active_run_extends_then_raises_after_max():
    b = _b(grace=120, ext=5, age=1300)
    b.check()                                   # ping 1 answered → +120s
    assert b.wc_extensions_used == 1
    assert b.max_wall_clock_s == 1320
    for want in (2, 3, 4, 5):
        b.started_at = time.monotonic() - (b.max_wall_clock_s + 1)
        b.check()                               # crosses the new deadline
        assert b.wc_extensions_used == want
    b.started_at = time.monotonic() - 1900      # past the extended deadline
    with pytest.raises(BudgetExceeded) as ei:
        b.check()
    assert ei.value.details["extensions_used"] == 5
    assert ei.value.details["limit"] == 1800


def test_extension_visible_in_summary():
    b = _b(grace=120, ext=5, age=1300)
    b.check()
    assert b.summary()["wall_clock_extensions"] == 1


def test_zero_wall_clock_never_extends():
    b = _b(grace=120, ext=5, wall=0, age=99999)
    b.check()                                   # 0 = no ceiling at all
    assert b.wc_extensions_used == 0
