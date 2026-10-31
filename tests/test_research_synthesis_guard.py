"""Extensive research triggers one evidence-preserving synthesis nudge."""
import asyncio
from types import SimpleNamespace

from runtime.budget import Budget
from runtime.run_state import RunState
from runtime.tool_base import ToolResult
from runtime.turn_guards import ResearchSynthesisGuard, ToolCallView


def _state():
    return RunState(budget=Budget(
        max_iterations=10, max_wall_clock_s=0.0,
        max_cost_usd=0.0, max_total_tokens=0))


def _guard(after=2):
    registry = SimpleNamespace(get=lambda name: object())
    tctx = SimpleNamespace(
        lg_cfg={"research_delegate_after": after}, depth=0,
        runtime=SimpleNamespace(registry=registry))
    return ResearchSynthesisGuard(tctx)


def _call():
    return ToolCallView(name="web.search", args={},
                        result=ToolResult(status="ok", result={}))


def test_research_synthesis_nudge_fires_once_with_evidence_instruction():
    rs = _state()
    rs.web_calls = 2
    guard = _guard()
    hint = asyncio.run(guard.check(rs, _call()))
    assert "specialist.delegate(strength='research')" in hint
    assert "Include the gathered evidence" in hint
    assert "do not restart the research from scratch" in hint
    rs.web_calls = 3
    assert asyncio.run(guard.check(rs, _call())) is None


def test_research_synthesis_nudge_skips_delegated_and_unavailable_runs():
    rs = _state()
    rs.web_calls = 2
    rs.delegated = True
    assert asyncio.run(_guard().check(rs, _call())) is None

    rs = _state()
    rs.web_calls = 2
    tctx = SimpleNamespace(
        lg_cfg={"research_delegate_after": 2}, depth=0,
        runtime=SimpleNamespace(registry=SimpleNamespace(get=lambda name: None)))
    assert asyncio.run(ResearchSynthesisGuard(tctx).check(rs, _call())) is None
