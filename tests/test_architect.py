"""The architect pipeline: plan → review → (arbitrate|refine) → handoff → execute,
driven over ctx.spawn. Stages are mocked to assert ordering/branching/parsing."""
import asyncio
from tools.agent.architect import Architect, _parse_stance, _parse_choice, _section
from runtime.tool_base import ToolContext


class _Ctx:
    def __init__(self, review_stance="agree", arb="CHOICE: B\nRATIONALE: cleaner."):
        self.config = {}
        self.calls = []
        self._stance = review_stance
        self._arb = arb
    async def spawn(self, task, *, tools=None, model=None, name=None, budget=None,
                    share_private=None, verify=None):
        self.calls.append({"name": name, "model": model, "verify": verify, "task": task})
        if name == "architect" and "REVIEWING" not in task and "Revise" not in task:
            return {"status": "ok", "answer": "GOAL: g\nAPPROACH: a\nUNITS: u1; u2"}
        if name == "architect" and "Revise" in task:
            return {"status": "ok", "answer": "GOAL: g2\nAPPROACH: revised\nUNITS: u1"}
        if name == "reviewer":
            body = {"agree": "STANCE: agree\nNOTES:", "refine": "STANCE: refine\nNOTES: fix x",
                    "disagree": "STANCE: disagree\nNOTES: no\nALTERNATIVE: do it differently"}[self._stance]
            return {"status": "ok", "answer": body}
        if name == "arbiter":
            return {"status": "ok", "answer": self._arb}
        if name == "executor":
            return {"status": "ok", "answer": "done", "verified": True, "files_changed": ["a.py"]}
        return {"status": "ok", "answer": ""}


def _run(ctx, args): return asyncio.run(Architect().execute(args, ctx))


def test_parsers():
    assert _parse_stance("STANCE: disagree\n...") == "disagree"
    assert _parse_stance("no marker") == "refine"
    assert _parse_choice("CHOICE: B\nRATIONALE: x") == "B"
    assert _section("GOAL: hi\nAPPROACH: yo", "GOAL") == "hi"


def test_agree_path_no_arbitration():
    ctx = _Ctx("agree")
    r = _run(ctx, {"task": "build X"})
    names = [c["name"] for c in ctx.calls]
    assert names == ["architect", "reviewer", "executor"]        # no arbiter
    assert r.result["arbitrated"] is False and r.result["executed"] is True
    assert "reviewer agreed" in r.result["handoff"]
    assert r.result["files_changed"] == ["a.py"]


def test_disagree_triggers_arbiter():
    ctx = _Ctx("disagree")
    r = _run(ctx, {"task": "build X"})
    names = [c["name"] for c in ctx.calls]
    assert names == ["architect", "reviewer", "arbiter", "executor"]
    assert r.result["arbitrated"] is True
    assert "Arbitration" in r.result["handoff"] and "approach B" in r.result["handoff"]
    # arbiter got a cloud model, not the local coder
    assert next(c for c in ctx.calls if c["name"] == "arbiter")["model"] == "claude"


def test_refine_revises_plan_no_arbiter():
    ctx = _Ctx("refine")
    r = _run(ctx, {"task": "build X"})
    names = [c["name"] for c in ctx.calls]
    assert names == ["architect", "reviewer", "architect", "executor"]  # revise, no arbiter
    assert r.result["arbitrated"] is False
    assert "revised" in r.result["handoff"]


def test_execute_false_stops_at_handoff():
    ctx = _Ctx("agree")
    r = _run(ctx, {"task": "build X", "execute": False})
    assert [c["name"] for c in ctx.calls] == ["architect", "reviewer"]  # no executor
    assert r.result["executed"] is False and "HANDOFF" in r.result["handoff"]


def test_reviewer_uses_coder_model():
    ctx = _Ctx("agree")
    _run(ctx, {"task": "x"})
    assert next(c for c in ctx.calls if c["name"] == "reviewer")["model"] == "local-coder"
