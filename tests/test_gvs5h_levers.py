"""GVS5H-inspired levers (arXiv:2608.26480), one test cluster per feature:

- note.set writes through to work_root/notes.md (the ledger on disk)
- cut-off sub-agent envelopes cap the partial + hint "simpler approach"
- agent.role_temperature pins per-role sampling onto delegated children
- agent.fresh_retry de-anchors a repeatedly-failing delegation (raw request,
  no orientation pack)
- eval.verify_gate attaches the case checker as a mid-run veto hook

Loop-level tests reuse the fake-model harness from test_loop_regressions.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from runtime.tool_base import ToolContext, ToolResult, cutoff_child_answer, role_sampling
from tools.agent.note import NoteSet, NOTES_FILENAME
from tools.agent.spawn import AgentSpawn
from tools.code.delegate import CodeDelegate

from test_loop_regressions import (CFG, _Registry, _StubTool, _final,
                                   _runtime, _spawn_rt, _tc)


def _ctx(cfg=None, work_root=None, spawn=None):
    return ToolContext(request_id="t", config=cfg or CFG, budget=None,
                       work_root=str(work_root) if work_root else None,
                       spawn=spawn)


# ---- notes ledger (feature 2) ----------------------------------------------

def test_note_set_writes_through_to_work_root(tmp_path):
    ctx = _ctx(work_root=tmp_path)
    ctx.set_note = lambda text: None
    res = asyncio.run(NoteSet().execute({"text": "goal: X\ndone: step 1"}, ctx))
    assert res.status == "ok"
    assert res.result["persisted"] == str(tmp_path / NOTES_FILENAME)
    assert (tmp_path / NOTES_FILENAME).read_text() == "goal: X\ndone: step 1\n"


def test_note_set_overwrites_and_survives_without_work_root(tmp_path):
    ctx = _ctx(work_root=None)
    ctx.set_note = lambda text: None
    res = asyncio.run(NoteSet().execute({"text": "no disk"}, ctx))
    assert res.status == "ok" and res.result["persisted"] is None


def test_note_description_teaches_curation():
    # The GVS5H ledger contract: rewrite, don't append; delete the superseded.
    assert "DELETE" in NoteSet.description and "notes.md" in NoteSet.description


# ---- cut-off child envelopes (feature 3) -----------------------------------

def test_cutoff_helper_passes_clean_child_through():
    child = {"status": "ok", "answer": "full answer"}
    answer, hint = cutoff_child_answer(child)
    assert answer == "full answer" and hint is None


def test_cutoff_helper_caps_and_hints():
    child = {"status": "budget_exceeded", "answer": "x" * 5000}
    answer, hint = cutoff_child_answer(child)
    assert len(answer) < 1700 and "truncated" in answer
    assert "simpler approach" in hint and "budget" in hint
    # stalled behaves the same
    _, hint2 = cutoff_child_answer({"status": "stalled", "answer": "y"})
    assert hint2


def test_spawn_envelope_caps_cutoff_child():
    async def fake_spawn(task, **kw):
        return {"status": "budget_exceeded", "answer": "partial " * 1000,
                "run_id": "c1", "budget": {}}
    ctx = _ctx(spawn=fake_spawn)
    res = asyncio.run(AgentSpawn().execute({"task": "build the thing"}, ctx))
    assert res.status == "error"
    assert "hint" in res.result and "simpler approach" in res.result["hint"]
    assert len(res.result["answer"]) < 1700


def test_delegate_envelope_caps_cutoff_child():
    async def fake_spawn(task, **kw):
        return {"status": "stalled", "answer": "half-done " * 1000,
                "run_id": "c2", "budget": {}}
    cfg = dict(CFG, tools={"code": {"delegate": {"model": "coder-alias"}}})
    ctx = _ctx(cfg=cfg, spawn=fake_spawn)
    res = asyncio.run(CodeDelegate().execute(
        {"task": "implement the parser", "fresh": True}, ctx))
    assert res.status == "error"
    assert "hint" in res.result and "simpler approach" in res.result["hint"]


# ---- per-role temperature (feature 4) ---------------------------------------

def test_role_sampling_helper():
    cfg = {"agent": {"role_temperature": {"coding": 0.2, "research": 0.4}}}
    assert role_sampling(cfg, "coding") == {"temperature": 0.2}
    assert role_sampling(cfg, "research") == {"temperature": 0.4}
    assert role_sampling(cfg, "vision") is None          # no entry → preset default
    assert role_sampling(cfg, None) is None
    assert role_sampling({"agent": {"role_temperature": {"coding": "junk"}},
                         }, "coding") is None            # unparsable → ignored
    assert role_sampling({"agent": {"role_temperature": {"coding": 9.9}}},
                         "coding") is None               # out of range → ignored
    assert role_sampling({}, "coding") is None           # feature off


def test_spawn_passes_role_temperature(monkeypatch):
    import tools.model.catalog as catalog
    seen = {}

    async def fake_route(config, tag):
        return "coder-alias"

    async def fake_spawn(task, **kw):
        seen.update(kw)
        return {"status": "ok", "answer": "done", "run_id": "c", "budget": {}}

    monkeypatch.setattr(catalog, "route_strength", fake_route)
    cfg = dict(CFG, agent={"role_temperature": {"coding": 0.2}})
    ctx = _ctx(cfg=cfg, spawn=fake_spawn)
    res = asyncio.run(AgentSpawn().execute(
        {"task": "implement x", "strength": "coding"}, ctx))
    assert res.status == "ok"
    assert seen["sampling"] == {"temperature": 0.2}
    assert seen["model"] == "coder-alias"


def test_delegate_passes_role_temperature_for_wanted_tag():
    seen = {}

    async def fake_spawn(task, **kw):
        seen.update(kw)
        return {"status": "ok", "answer": "done", "run_id": "c", "budget": {}}

    cfg = dict(CFG,
               tools={"code": {"delegate": {"model": "coder-alias"}}},
               agent={"role_temperature": {"security": 0.2}})
    ctx = _ctx(cfg=cfg, spawn=fake_spawn)
    res = asyncio.run(CodeDelegate().execute(
        {"task": "audit the parser", "strength": "security", "fresh": True}, ctx))
    assert res.status == "ok"
    assert seen["sampling"] == {"temperature": 0.2}


def test_ctx_spawn_threads_sampling_into_child_run():
    """Loop level: sampling= on ctx.spawn becomes the child's run_overrides
    (sampling + sampling_force, the pin that reaches a specialist alias)."""
    captured = {}

    async def child(msg, **kw):
        captured.update(kw)
        return {"status": "ok", "answer": "x", "run_id": "s", "budget": {}}

    rt, _ = _spawn_rt([_tc("agent.spawn", json.dumps(
        {"task": "t", "strength": "coding"})), _final("wrapped")], child)
    rt._local_aliases = frozenset({"coder-alias"})   # no cloud gate
    import tools.model.catalog as catalog
    orig = catalog.route_strength

    async def fake_route(config, tag):
        return "coder-alias"

    catalog.route_strength = fake_route
    rt.config = dict(CFG, agent={"role_temperature": {"coding": 0.2}},
                     # the routed alias isn't a configured local alias in this
                     # fixture — switch off the cloud-confirm gate instead
                     confirmation={"confirm_cloud_calls": False})
    try:
        out = asyncio.run(rt.run("delegate this"))
    finally:
        catalog.route_strength = orig
    assert out["status"] == "ok"
    assert captured["run_overrides"] == {"sampling": {"temperature": 0.2},
                                         "sampling_force": True}


def test_ctx_spawn_without_sampling_sends_no_overrides():
    captured = {}

    async def child(msg, **kw):
        captured.update(kw)
        return {"status": "ok", "answer": "x", "run_id": "s", "budget": {}}

    rt, _ = _spawn_rt([_tc("agent.spawn", json.dumps({"task": "t"})),
                       _final("wrapped")], child)
    out = asyncio.run(rt.run("spawn plain"))
    assert out["status"] == "ok"
    assert captured.get("run_overrides") is None


# ---- fresh-perspective retry (feature 5) -------------------------------------

class _FailTwice:
    """code.delegate stand-in: fails twice, then succeeds; records args."""
    private = False
    name = "code.delegate"

    def __init__(self):
        self.seen = []
        self._fails = 2

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name,
                                                 "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        self.seen.append(dict(args))
        if self._fails > 0:
            self._fails -= 1
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="child died: budget_exceeded")
        return ToolResult(status="ok", tool_name=self.name,
                          result={"status": "ok", "answer": "fixed"})


def _fresh_rt(tool, script):
    rt, seen = _runtime(_Registry([], real={"code.delegate": tool}), script)
    rt.config = dict(CFG, agent={"fresh_retry": {"enabled": True, "after": 2}})
    return rt, seen


def test_fresh_retry_rewrites_third_attempt():
    tool = _FailTwice()
    task1 = "implement the parser for the config format in the project"
    rt, _ = _fresh_rt(tool, [
        _tc("code.delegate", json.dumps({"task": task1})),
        _tc("code.delegate", json.dumps({"task": task1 + ", first try"})),
        _tc("code.delegate", json.dumps({"task": task1 + ", second try"})),
        _final("all done")])
    out = asyncio.run(rt.run("build the config parser please"))
    assert out["status"] == "ok"
    assert len(tool.seen) == 3
    assert "FRESH RETRY" not in tool.seen[0]["task"]
    assert "FRESH RETRY" not in tool.seen[1]["task"]
    # Third attempt at the same cluster: de-anchored — raw original request,
    # fresh=True for the orientation-pack skip.
    assert tool.seen[2]["task"].startswith("FRESH RETRY")
    assert "build the config parser please" in tool.seen[2]["task"]
    assert tool.seen[2].get("fresh") is True


def test_fresh_retry_disabled_leaves_task_untouched():
    tool = _FailTwice()
    rt, _ = _fresh_rt(tool, [
        _tc("code.delegate", json.dumps({"task": "implement the parser"})),
        _tc("code.delegate", json.dumps({"task": "implement the parser now"})),
        _tc("code.delegate", json.dumps({"task": "implement the parser please"})),
        _final("all done")])
    rt.config["agent"]["fresh_retry"]["enabled"] = False
    out = asyncio.run(rt.run("build it"))
    assert out["status"] == "ok"
    assert all("FRESH RETRY" not in a["task"] for a in tool.seen)


def test_delegate_fresh_skips_orientation_pack(monkeypatch):
    import runtime.context_pack as cp
    calls = []

    def fake_pack(work_root, config):
        calls.append(work_root)
        return "ORIENTATION PACK"

    async def fake_spawn(task, **kw):
        calls.append(task)
        return {"status": "ok", "answer": "done", "run_id": "c", "budget": {}}

    monkeypatch.setattr(cp, "coding_context", fake_pack)
    cfg = dict(CFG, tools={"code": {"delegate": {"model": "coder-alias"}}})
    ctx = _ctx(cfg=cfg, spawn=fake_spawn)
    asyncio.run(CodeDelegate().execute({"task": "t1"}, ctx))
    assert calls[0] is None or calls[0] == ""  # pack consulted (work_root None)
    assert calls[1].startswith("ORIENTATION PACK")
    calls.clear()
    asyncio.run(CodeDelegate().execute({"task": "t2", "fresh": True}, ctx))
    assert calls == ["t2"]                     # pack never consulted, task raw


# ---- correctness veto hook (feature 1) ---------------------------------------

def test_normalize_verify_hook_spec():
    from runtime.loop import AgentRuntime

    async def hook(answer):
        return True, "green"

    spec = AgentRuntime._normalize_verify(
        type("S", (), {"config": {}})(), {"hook": hook, "command": "eval:demo"})
    assert spec["hook"] is hook and spec["command"] == "eval:demo"
    assert spec["protect"] == [] and spec["max_checks"] == 4


def test_hook_veto_bounces_done_until_green():
    calls = []

    async def hook(answer):
        calls.append(answer)
        if len(calls) < 2:
            return False, "case checker FAILED: test_x is red"
        return True, "case checker passed"

    rt, seen = _runtime(_Registry(["fs.read"]),
                        [_final("first attempt"), _final("fixed it")])
    out = asyncio.run(rt.run("solve the case",
                             verify={"hook": hook, "command": "eval:demo"}))
    assert out["status"] == "ok" and out["answer"] == "fixed it"
    assert out["verified"] is True
    assert calls == ["first attempt", "fixed it"]
    # The veto report went back into the conversation as a user message.
    assert any("NOT complete" in str(m.get("content"))
               and "test_x is red" in str(m.get("content"))
               for m in seen[-1])


def test_hook_veto_gives_up_unverified():
    n = 0

    async def hook(answer):
        nonlocal n
        n += 1
        return False, f"red attempt {n}"   # distinct reports: no stall shortcut

    rt, _ = _runtime(_Registry(["fs.read"]),
                     [_final("a"), _final("b"), _final("c")])
    out = asyncio.run(rt.run("solve it",
                             verify={"hook": hook, "max_checks": 2}))
    assert out["status"] == "unverified"
    assert "NOT VERIFIED" in out["answer"] and out["verified"] is False
    assert n == 2
