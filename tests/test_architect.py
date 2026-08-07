"""The architect pipeline: plan → review → (arbitrate|refine) → handoff → execute,
driven over ctx.spawn. Stages are mocked to assert ordering/branching/parsing."""
import asyncio
from tools.agent.architect import Architect, _parse_stance, _parse_choice, _section
from runtime.tool_base import ToolContext


class _Ctx:
    def __init__(self, review_stance="agree", arb="CHOICE: B\nRATIONALE: cleaner.",
                 arb_status="ok"):
        self.config = {}
        self.calls = []
        self._stance = review_stance
        self._arb = arb
        self._arb_status = arb_status
    async def spawn(self, task, *, tools=None, model=None, name=None, budget=None,
                    share_private=None, verify=None, todos_sync=False):
        self.calls.append({"name": name, "model": model, "verify": verify,
                           "todos_sync": todos_sync, "task": task})
        if name == "architect" and "REVIEWING" not in task and "Revise" not in task:
            return {"status": "ok", "answer": "GOAL: g\nAPPROACH: a\nUNITS: u1; u2"}
        if name == "architect" and "Revise" in task:
            return {"status": "ok", "answer": "GOAL: g2\nAPPROACH: revised\nUNITS: u1"}
        if name == "reviewer":
            body = {"agree": "STANCE: agree\nNOTES:", "refine": "STANCE: refine\nNOTES: fix x",
                    "disagree": "STANCE: disagree\nNOTES: no\nALTERNATIVE: do it differently"}[self._stance]
            return {"status": "ok", "answer": body}
        if name == "arbiter":
            if self._arb_status != "ok":
                return {"status": self._arb_status, "answer": "", "error": "LiteLLM 400"}
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
    # Only the executor takes over the parent's todo list (audit T3).
    assert [c["todos_sync"] for c in ctx.calls] == [False, False, True]


def test_disagree_triggers_arbiter():
    ctx = _Ctx("disagree")
    r = _run(ctx, {"task": "build X"})
    names = [c["name"] for c in ctx.calls]
    assert names == ["architect", "reviewer", "arbiter", "executor"]
    assert r.result["arbitrated"] is True
    assert "Arbitration" in r.result["handoff"] and "approach B" in r.result["handoff"]
    # the default friendly alias resolves to its LiteLLM alias, not the local specialist
    assert next(c for c in ctx.calls if c["name"] == "arbiter")["model"] == "kimi-k3"


def test_disagree_arbiter_failure_is_loud_not_silent():
    # Previously a failed arbiter call fell through to "## Review — the reviewer
    # agreed with the plan" in the handoff. Now the dissent + failure are explicit.
    ctx = _Ctx("disagree", arb_status="error")
    r = _run(ctx, {"task": "build X"})
    assert r.status == "ok"                      # plan A still executes
    assert r.result["arbitrated"] is False
    assert r.result["arbitration_error"]
    handoff = r.result["handoff"]
    assert "arbitration failed" in handoff.lower()
    assert "reviewer agreed" not in handoff
    assert "do it differently" in handoff        # reviewer's alternative stays visible


def test_unknown_reviewer_model_fails_fast():
    ctx = _Ctx("agree")
    ctx.config = {"architect": {"reviewer_model": "no-such-model"}}
    r = _run(ctx, {"task": "x"})
    assert r.status == "error" and "reviewer_model" in r.error
    assert ctx.calls == []                       # nothing spawned


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


def test_reviewer_uses_specialist_model():
    ctx = _Ctx("agree")
    _run(ctx, {"task": "x"})
    assert next(c for c in ctx.calls if c["name"] == "reviewer")["model"] == "local-specialist"
