"""Mechanical architect routing: complex work starts automatically, trivial
work stays direct, and a triggered failure cannot fall through to inline code."""
import asyncio
from types import SimpleNamespace

import pytest
from test_loop_regressions import _final, _Registry, _runtime

from runtime.plan_gate import PlanDecision, choose_plan, maybe_auto_architect
from runtime.tool_base import ToolResult


def test_trivial_and_threshold_off_stay_direct():
    assert choose_plan("fix a typo in README.md").action == "direct"
    assert choose_plan("design and implement a new feature", threshold=0).action == "direct"


@pytest.mark.parametrize("message", [
    "explain the design document for the scheduler",
    "write a blog post about our new feature",
    "review the data migration script and summarize risks",
    "what does the schema migration in alembic do?",
    "plan a birthday party for my kid",
    "summarize the multi-file diff from yesterday",
    "document the new feature flags in the README",
    "explain how the irreversible decision rule works",
    "what does force-push do?",
])
def test_benign_descriptive_and_non_software_requests_stay_direct(message):
    assert choose_plan(message).action == "direct"


def test_complex_and_explicit_plan_actions():
    complex_ = choose_plan("design and implement a new feature across the API")
    assert complex_.action == "execute" and not complex_.mandatory
    planned = choose_plan("Plan the migration before changing anything")
    assert planned.action == "plan"


def test_direct_bypasses_only_discretionary_planning():
    assert choose_plan("design and implement a new feature", mode="direct").action == "direct"
    risky = choose_plan("deploy to production now", mode="direct")
    assert risky.action == "plan" and risky.mandatory
    migration = choose_plan("apply the schema migration", mode="direct")
    assert migration.action == "plan" and migration.mandatory


def test_plan_mode_forces_plan_only():
    assert choose_plan("fix a typo", mode="plan").action == "plan"


class _Runtime:
    def __init__(self, status="ok"):
        self.registry = SimpleNamespace(get=lambda name: object())
        self.status = status
        self.calls = []

    async def _execute_tool(self, name, args, ctx):
        self.calls.append((name, args, ctx))
        if self.status != "ok":
            return ToolResult(status="error", result=None, tool_name=name,
                              error="review failed")
        return ToolResult(status="ok", tool_name=name,
                          result={"answer": "verified result", "handoff": "HANDOFF"})


def _rs():
    return SimpleNamespace(tools_used=[], trajectory=[], final_answer="", status="ok",
                           error_msg="", mutation_gen=0, allowed=["architect"],
                           budget=SimpleNamespace(iterations=0))


def _run(runtime, decision, allowed=None):
    rs = _rs()
    if allowed is not None:
        rs.allowed = allowed
    events = []

    async def emit(kind, iteration, payload):
        events.append((kind, payload))

    handled = asyncio.run(maybe_auto_architect(
        runtime, decision, user_message="original task", depth=0, ctx=object(),
        rs=rs, emit=emit))
    return handled, rs, events


def test_auto_architect_runs_harness_side_and_finishes_parent():
    runtime = _Runtime()
    handled, rs, events = _run(runtime, PlanDecision("execute", "complex implementation"))
    assert handled and rs.status == "ok" and rs.final_answer == "verified result"
    assert runtime.calls[0][1] == {"task": "original task", "execute": True,
                                   "review": "egm"}
    assert rs.tools_used == ["architect"]
    assert {kind for kind, _ in events} == {
        "plan_gate", "tool_result", "architect_autostart"}


def test_triggered_failure_fails_closed():
    runtime = _Runtime("error")
    handled, rs, _ = _run(runtime, PlanDecision("plan", "mandatory", True))
    assert handled and rs.status == "error" and "review failed" in rs.final_answer
    assert runtime.calls[0][1]["execute"] is False


def test_direct_decision_does_not_call_architect():
    runtime = _Runtime()
    handled, _, events = _run(runtime, PlanDecision())
    assert not handled and not runtime.calls and not events


def test_disabled_architect_fails_closed_instead_of_bypassing_allowlist():
    runtime = _Runtime()
    handled, rs, _ = _run(runtime, PlanDecision("execute", "complex"),
                          allowed=["fs.write"])
    assert handled and rs.status == "error" and not runtime.calls
    assert "unavailable" in rs.error_msg


class _ArchitectTool:
    name = "architect"
    private = False

    def __init__(self):
        self.args = []

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {
            "name": self.name, "description": "", "parameters": {}}}

    async def execute(self, args, ctx):
        self.args.append(args)
        return ToolResult(status="ok", tool_name=self.name,
                          result={"answer": "planned and verified", "executed": True})


def test_real_loop_auto_architect_skips_parent_model_turn():
    architect = _ArchitectTool()
    rt, seen = _runtime(_Registry(["fs.write"], real={"architect": architect}), [])
    rt.config["architect"] = {"mode": "auto", "threshold": 4}
    rt.skills = {}
    out = asyncio.run(rt.run("design and implement a new feature"))
    assert out["status"] == "ok" and out["answer"] == "planned and verified"
    assert architect.args[0]["review"] == "egm" and architect.args[0]["execute"] is True
    assert seen == []                       # no model-led inline fallback


def test_real_loop_direct_mode_stays_direct_but_not_for_risk():
    architect = _ArchitectTool()
    rt, seen = _runtime(_Registry(["fs.write"], real={"architect": architect}),
                        [_final("small change done")])
    rt.config["architect"] = {"mode": "direct", "threshold": 4}
    out = asyncio.run(rt.run("design and implement a new feature"))
    assert out["answer"] == "small change done" and not architect.args and len(seen) == 1

    risky_architect = _ArchitectTool()
    rt, seen = _runtime(_Registry(["fs.write"], real={"architect": risky_architect}), [])
    rt.config["architect"] = {"mode": "direct", "threshold": 4}
    out = asyncio.run(rt.run("deploy to production now"))
    assert out["answer"] == "planned and verified" and not seen
    assert risky_architect.args[0]["execute"] is False
