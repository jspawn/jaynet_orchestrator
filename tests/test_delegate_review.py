"""Delegation review (agent.verify_delegate_review, default on): judgment of a
finished delegation moves off the brain onto the strongest available model —
a live verify-tagged slot → the specialist that did the work → the allround
slot, never the brain. The reviewer sees only task + report + evidence and
answers {verdict, issues}; a 'fail' attaches a loud review_warning. Advisory:
the deterministic verified flag is unchanged. Skipped when the child ran on
the brain or errored, and never fatal when no review alias answers.
"""
from __future__ import annotations

import asyncio

import runtime.verify as V
import tools.model.catalog as catalog
from runtime.tool_base import ToolContext
from tests.test_loop_regressions import CFG
from tools.specialist.delegate import SpecialistDelegate


def _ctx(tmp_path, *, agent_cfg=None, model="coder-alias", spawn=None):
    if spawn is None:
        async def spawn(task, **kw):
            return {"status": "ok", "answer": "implemented and tested",
                    "run_id": "c", "budget": {}, "verified": True,
                    "verify_command": "pytest -q",
                    "files_changed": ["parser.py"]}
    cfg = dict(CFG, tools={"code": {"delegate": {"model": model},
                                    "run": {"sandbox_prefix": []}}})
    if agent_cfg is not None:
        cfg["agent"] = agent_cfg
    return ToolContext(request_id="t", config=cfg, budget=None,
                       work_root=str(tmp_path), spawn=spawn)


def _delegate(ctx):
    return asyncio.run(SpecialistDelegate().execute(
        {"task": "implement the parser", "fresh": True}, ctx))


def _fake_call(content):
    async def call(config, alias, messages):
        return {"status": "ok", "content": content, "served_model": alias,
                "error": None}
    return call


def test_review_attached_on_ok_child(tmp_path, monkeypatch):
    async def no_route(config, wanted):
        return None
    monkeypatch.setattr(catalog, "route_strength", no_route)
    monkeypatch.setattr(V, "_default_review_call", _fake_call(
        '{"verdict": "pass", "issues": [], "confidence": 0.9}'))
    res = _delegate(_ctx(tmp_path))
    assert res.status == "ok"
    review = res.result["review"]
    assert review["verdict"] == "pass" and review["confidence"] == 0.9
    assert review["model"] == "coder-alias"      # fell to the working model
    assert "review_warning" not in res.result


def test_review_fail_adds_warning(tmp_path, monkeypatch):
    async def no_route(config, wanted):
        return None
    monkeypatch.setattr(catalog, "route_strength", no_route)
    monkeypatch.setattr(V, "_default_review_call", _fake_call(
        '{"verdict": "fail", "issues": ["no edge-case handling"]}'))
    res = _delegate(_ctx(tmp_path))
    assert res.result["review"]["verdict"] == "fail"
    assert "no edge-case handling" in res.result["review_warning"]
    # Small brains took "verify yourself" as license to edit inline and
    # spiraled (live: Spark 4B delegate-strength-routing) — the warning must
    # point at re-delegation and read-only verification only.
    assert "re-delegate" in res.result["review_warning"]
    assert "read-only" in res.result["review_warning"]
    assert "do NOT fix files inline" in res.result["review_warning"]
    # Advisory: the deterministic verified flag and the ok status survive.
    assert res.result["verified"] is True and res.status == "ok"


def test_verify_tag_route_wins(tmp_path, monkeypatch):
    async def route(config, wanted):
        return "verify-alias" if wanted == "verify" else None
    monkeypatch.setattr(catalog, "route_strength", route)
    seen = []

    async def call(config, alias, messages):
        seen.append(alias)
        return {"status": "ok",
                "content": '{"verdict": "pass", "issues": []}',
                "served_model": alias, "error": None}
    monkeypatch.setattr(V, "_default_review_call", call)
    res = _delegate(_ctx(tmp_path))
    assert seen == ["verify-alias"]
    assert res.result["review"]["model"] == "verify-alias"


def test_no_review_when_child_ran_on_brain(tmp_path, monkeypatch):
    called = []

    async def call(config, alias, messages):
        called.append(alias)
        return {"status": "ok", "content": '{"verdict": "pass"}',
                "served_model": alias, "error": None}
    monkeypatch.setattr(V, "_default_review_call", call)
    res = _delegate(_ctx(tmp_path, model=None))
    assert called == [] and "review" not in res.result


def test_no_review_when_child_errored(tmp_path, monkeypatch):
    called = []

    async def call(config, alias, messages):
        called.append(alias)
        return {"status": "ok", "content": '{"verdict": "pass"}',
                "served_model": alias, "error": None}
    monkeypatch.setattr(V, "_default_review_call", call)

    async def spawn(task, **kw):
        return {"status": "max_iterations", "answer": "", "run_id": "c",
                "budget": {}, "verified": None, "error": "max_iterations"}
    res = _delegate(_ctx(tmp_path, spawn=spawn))
    assert res.status == "error"
    assert called == [] and "review" not in (res.result or {})


def test_review_disabled_by_config(tmp_path, monkeypatch):
    called = []

    async def call(config, alias, messages):
        called.append(alias)
        return {"status": "ok", "content": '{"verdict": "pass"}',
                "served_model": alias, "error": None}
    monkeypatch.setattr(V, "_default_review_call", call)
    res = _delegate(_ctx(tmp_path,
                         agent_cfg={"verify_delegate_review": False}))
    assert res.status == "ok"
    assert called == [] and "review" not in res.result


def test_review_never_fatal(tmp_path, monkeypatch):
    async def no_route(config, wanted):
        return None
    monkeypatch.setattr(catalog, "route_strength", no_route)

    async def dead(config, alias, messages):
        return {"status": "error", "content": "", "served_model": "",
                "error": "connection refused"}
    monkeypatch.setattr(V, "_default_review_call", dead)
    res = _delegate(_ctx(tmp_path))
    assert res.status == "ok" and "review" not in res.result


def test_unparseable_verdict_skipped(tmp_path, monkeypatch):
    async def no_route(config, wanted):
        return None
    monkeypatch.setattr(catalog, "route_strength", no_route)
    monkeypatch.setattr(V, "_default_review_call",
                        _fake_call("I think it looks fine overall."))
    res = _delegate(_ctx(tmp_path))
    assert res.status == "ok" and "review" not in res.result


def test_tainted_run_drops_cloud_aliases():
    """Audit #24 C2: the review payload (task + report + evidence) must not
    leave the box ungated — a tainted run restricts the alias chain to local
    models (tools have no per-call confirm seam; fail safe)."""
    seen = []

    async def call(config, alias, messages):
        seen.append(alias)
        return {"status": "ok", "content": '{"verdict": "pass", "issues": []}',
                "served_model": alias, "error": None}
    out = asyncio.run(V.review_delegation(
        "task", "report", {}, CFG,
        aliases=["glm-cloud", "local-specialist"], call=call,
        private_taint=True, share_private=False))
    assert seen == ["local-specialist"] and out["verdict"] == "pass"
    # An untainted run keeps the full chain, and explicit sharing re-allows it.
    seen.clear()
    asyncio.run(V.review_delegation(
        "task", "report", {}, CFG,
        aliases=["glm-cloud", "local-specialist"], call=call,
        private_taint=True, share_private=True))
    assert seen == ["glm-cloud"]


def test_tainted_run_all_cloud_skips_review():
    seen = []

    async def call(config, alias, messages):
        seen.append(alias)
        return {"status": "ok", "content": '{"verdict": "pass"}',
                "served_model": alias, "error": None}
    out = asyncio.run(V.review_delegation(
        "task", "report", {}, CFG, aliases=["glm-cloud", "kimi"],
        call=call, private_taint=True, share_private=False))
    assert out is None and seen == []


# ---- authored-check lint (delta-fail investigation 2026-10-06) ---------------
# tb-huarong-dao-solver: verified:true on `assert b[-1]==[...] or True` (the
# solution was invalid); gaia-65afbc8a: verified:true on
# `python3 -c "print('ok')"` with files_changed: []. A check that cannot
# fail — or cannot be about the deliverable — fails the review WITHOUT a
# model call.

def _review(task, evidence, call=None):
    if call is None:
        call = _fake_call('{"verdict": "pass", "issues": [],'
                          ' "confidence": 0.9}')
    return asyncio.run(V.review_delegation(
        task, "done, verified", evidence, {}, aliases=["spec"], call=call))


def _ac(command):
    return {"authored_check": {"command": command, "exit_code": 0,
                               "verified": True, "output": "ok"}}


class _NoCall:
    def __init__(self):
        self.called = False

    async def __call__(self, config, alias, messages):
        self.called = True
        return {"status": "ok", "content": '{"verdict": "pass", "issues": []}',
                "served_model": alias, "error": None}


def test_lint_tautology_or_true_fails_without_model_call():
    task = "Solve the puzzle and write the solution to solution.json"
    ev = _ac("python3 -c \"import json; b=json.load(open('solution.json'));"
             " assert b[-1]==[2,3] or True\"")
    ev["files_changed"] = ["solution.json"]
    nc = _NoCall()
    out = _review(task, ev, call=nc)
    assert out["verdict"] == "fail"
    assert out["model"] == "authored-check-lint"
    assert not nc.called                       # no model call spent
    assert any("or True" in i for i in out["issues"])


def test_lint_print_only_check_fails():
    nc = _NoCall()
    out = _review("Write the answer to answer.json",
                  {**_ac("python3 -c \"print('ok')\""), "files_changed": []},
                  call=nc)
    assert out["verdict"] == "fail" and not nc.called
    assert any("print-only" in i for i in out["issues"])


def test_lint_shell_or_true_and_assert_true_fail():
    for cmd in ("pytest -q || true", "python3 -c \"assert True\""):
        out = _review("fix the bug", {**_ac(cmd), "files_changed": ["x.py"]})
        assert out["verdict"] == "fail", cmd
        assert out["model"] == "authored-check-lint"


def test_lint_check_referencing_none_of_the_deliverables_fails():
    task = "Solve it and write the moves to solution.json"
    # A REAL assertion, but about nothing the task named as deliverable.
    out = _review(task, {**_ac("python3 -c \"assert len(open('moves.txt')"
                               ".read()) > 0\""),
                         "files_changed": ["solution.json"]})
    assert out["verdict"] == "fail"
    assert any("references none" in i for i in out["issues"])


def test_lint_genuine_check_passes_through_to_the_model():
    task = "Solve it and write the moves to solution.json"
    nc = _NoCall()
    out = _review(task, {**_ac("python3 -c \"import json; s=json.load("
                               "open('solution.json')); assert s['moves']"
                               " == ['up','left']\""),
                         "files_changed": ["solution.json"]},
                  call=nc)
    assert nc.called                           # the model grades it
    assert out["verdict"] == "pass" and out["model"] == "spec"


def test_missing_deliverable_in_files_changed_is_flagged_not_failed():
    """files_changed can miss shell-written files — a genuine check passes
    the lint, and the gap is a flag in the evidence the model grades."""
    task = "Solve it and write the moves to solution.json"
    seen = {}

    async def capture(config, alias, messages):
        seen["user"] = messages[-1]["content"]
        return {"status": "ok",
                "content": '{"verdict": "pass", "issues": []}',
                "served_model": alias, "error": None}

    out = _review(task, {**_ac("python3 -c \"import json; assert json.load("
                               "open('solution.json'))['ok']\""),
                         "files_changed": []},
                  call=capture)
    assert out["verdict"] == "pass"            # lint did not fire
    assert "solution.json" in seen["user"]
    assert "files_changed does not include" in seen["user"]
