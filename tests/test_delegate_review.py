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

from test_loop_regressions import CFG

import runtime.verify as V
import tools.model.catalog as catalog
from runtime.tool_base import ToolContext
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
