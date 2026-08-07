"""Sub-agent budget assembly (agent.spawn): config default_budget + clamping.

Precedence per dimension: spawn call `req` > config `db` (agent.default_budget) >
parent's remaining allowance (cost/tokens/wall) or default_sub_iterations. Cost,
tokens and wall are clamped to the parent's remaining so a child can't out-spend it.
"""
from runtime.loop import _child_budget

REM_COST, REM_TOK, REM_WALL = 1.0, 500_000, 300.0
DEFAULT_ITERS = 8


def test_no_config_no_req_falls_back_to_parent_remaining():
    b = _child_budget(None, None, DEFAULT_ITERS, REM_COST, REM_TOK, REM_WALL)
    assert b["max_cost_usd"] == REM_COST
    assert b["max_total_tokens"] == REM_TOK
    assert b["max_wall_clock_s"] == REM_WALL
    assert b["max_iterations"] == DEFAULT_ITERS      # -> default_sub_iterations


def test_config_default_budget_used_when_call_omits_budget():
    db = {"max_iterations": 20, "max_total_tokens": 200_000,
          "max_wall_clock_s": 120, "max_cost_usd": 0.5}
    b = _child_budget({}, db, DEFAULT_ITERS, REM_COST, REM_TOK, REM_WALL)
    assert b["max_iterations"] == 20                 # db beats default_sub_iterations
    assert b["max_total_tokens"] == 200_000
    assert b["max_wall_clock_s"] == 120
    assert b["max_cost_usd"] == 0.5


def test_per_call_req_overrides_config():
    db = {"max_iterations": 20, "max_total_tokens": 200_000}
    req = {"max_iterations": 3, "max_total_tokens": 50_000}
    b = _child_budget(req, db, DEFAULT_ITERS, REM_COST, REM_TOK, REM_WALL)
    assert b["max_iterations"] == 3                  # req wins over db
    assert b["max_total_tokens"] == 50_000


def test_cost_tokens_wall_clamped_to_parent_remaining():
    # config (or a caller) asking for more than the parent has left is capped
    db = {"max_total_tokens": 10_000_000, "max_wall_clock_s": 9_999, "max_cost_usd": 99.0}
    b = _child_budget({}, db, DEFAULT_ITERS, REM_COST, REM_TOK, REM_WALL)
    assert b["max_total_tokens"] == REM_TOK
    assert b["max_wall_clock_s"] == REM_WALL
    assert b["max_cost_usd"] == REM_COST


def test_iterations_not_clamped_to_parent_remaining():
    # iterations are per-run; a big child iteration budget is allowed even though
    # cost/tokens/wall remain clamped to the parent's remaining allowance.
    b = _child_budget({"max_iterations": 1000}, None, DEFAULT_ITERS,
                      REM_COST, REM_TOK, REM_WALL)
    assert b["max_iterations"] == 1000


def test_partial_config_leaves_other_dims_on_parent_remaining():
    # only iterations configured; the rest still track the parent's remaining
    db = {"max_iterations": 12}
    b = _child_budget({}, db, DEFAULT_ITERS, REM_COST, REM_TOK, REM_WALL)
    assert b["max_iterations"] == 12
    assert b["max_cost_usd"] == REM_COST
    assert b["max_total_tokens"] == REM_TOK
    assert b["max_wall_clock_s"] == REM_WALL


# The web UI sends a per-run `sub_budget`; loop.py merges it over config
# default_budget as {**config, **run} before calling _child_budget. These two
# lock the resulting precedence: spawn-call budget > run sub_budget > config.
def test_run_sub_budget_beats_config_default_budget():
    merged = {**{"max_iterations": 8}, **{"max_iterations": 40}}   # config, then run
    b = _child_budget({}, merged, DEFAULT_ITERS, REM_COST, REM_TOK, REM_WALL)
    assert b["max_iterations"] == 40


def test_spawn_call_budget_still_beats_run_override():
    merged = {"max_iterations": 40}                               # config+run merged
    b = _child_budget({"max_iterations": 5}, merged, DEFAULT_ITERS,
                      REM_COST, REM_TOK, REM_WALL)
    assert b["max_iterations"] == 5


def test_disabled_parent_dims_inherit_unlimited():
    # A remaining allowance of 0 means the parent's dimension is DISABLED
    # (Budget.check reads 0 as "no ceiling"): the child inherits "unlimited"
    # per dimension — and an explicit child cap is not clobbered by it.
    # (An ENABLED-but-exhausted parent is refused at the spawn call site —
    # see test_loop_regressions.py — so 0 here only ever means "disabled".)
    b = _child_budget({}, {}, DEFAULT_ITERS, 0.0, 0, 0.0)
    assert b["max_cost_usd"] == 0.0
    assert b["max_total_tokens"] == 0
    assert b["max_wall_clock_s"] == 0.0
    b = _child_budget({"max_cost_usd": 0.5, "max_total_tokens": 1000}, {},
                      DEFAULT_ITERS, 0.0, 0, 0.0)
    assert b["max_cost_usd"] == 0.5
    assert b["max_total_tokens"] == 1000
