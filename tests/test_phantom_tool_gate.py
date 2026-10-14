"""Follow-up context fixes (live 2026-10-14, run 7c8a2518):

A "count the people in the erp demo" run loaded the erp_demo connector and
worked; the follow-up "lookup all users named Christian Fischer…" carried no
keyword, so per-message selection dropped the namespace. The brain knew
erp_demo.search from the transcript, couldn't call it, and burned 24 turns
trying to execute it as shell/python code (cancelled by hand).

Two fixes, tested here:

1. History-aware selection — the last few turns' text joins keyword matching
   (tool_selection.history_turns), so a loaded namespace stays sticky.
2. PhantomToolGate — a call-shaped reference to a KNOWN tool that isn't in
   the run's allowlist, inside code.check/code.run/code.execute args, is
   refused with the actionable tools.load recovery instead of executing.
"""
from __future__ import annotations

import asyncio
import json

from runtime.selector import ToolSelector
from runtime.tool_base import ToolResult
from tests.test_loop_regressions import CFG, _final, _Registry, _runtime, _StubTool, _tc

# ---- 1. history-aware keyword selection ------------------------------------


def _erp_sel(cfg_extra=None):
    cfg = {"tool_selection": {
        "mode": "auto",
        "core_namespaces": ["web.search", "tools.load"],
        "keyword_namespaces": {"erp_demo": ["erp", "student"]},
    }}
    cfg["tool_selection"].update(cfg_extra or {})
    return ToolSelector(
        _Registry(["web.search", "tools.load", "erp_demo.search",
                   "erp_demo.get", "erp_demo.count"]), cfg)


def test_history_hint_keeps_namespace_sticky():
    s = _erp_sel()
    out = s.select("now give me their email addresses",
                   history_hint="can you count the people in the erp demo?")
    assert out is not None
    assert "erp_demo.search" in out
    assert s._diag["kw_triggered"] == {"erp_demo": "erp (history)"}
    # A history hit must suppress the trivial fast-path strip, too.
    assert s._diag["trivial"] is False


def test_no_history_no_namespace():
    s = _erp_sel()
    out = s.select("now give me their email addresses")
    assert out is not None and "erp_demo.search" not in out


def test_message_keyword_still_wins_without_history():
    s = _erp_sel()
    out = s.select("lookup the student record")
    assert "erp_demo.search" in out
    assert s._diag["kw_triggered"] == {"erp_demo": "student"}


def test_history_hint_none_is_previous_behavior():
    s = _erp_sel()
    assert s.select("hi", history_hint=None) == s.select("hi")


def test_loop_passes_history_to_selector():
    """Wiring: run(history=...) feeds the last turns into select()."""
    reg = _Registry(["web.search", "web.fetch", "ask.user", "skill.load",
                     "note.set", "todos", "run.badge", "deliver.files",
                     "memory.search", "fs.list", "fs.read", "llm.call",
                     "tools.load", "erp_demo.search", "erp_demo.get"])
    rt, _ = _runtime(reg, [_final("ok")])
    cfg = dict(CFG)
    cfg["tool_selection"] = {
        "mode": "auto",
        "keyword_namespaces": {"erp_demo": ["erp"]},
        "history_turns": 3,
    }
    rt.config = cfg
    rt.selector = ToolSelector(reg, cfg)
    out = asyncio.run(rt.run(
        "now their emails please",
        history=[{"role": "user", "content": "count the people in the erp"},
                 {"role": "assistant", "content": "28,931 people"}]))
    assert out["status"] == "ok"
    # The selector saw the history hint and kept the namespace sticky.
    assert rt.selector._diag["kw_triggered"] == {"erp_demo": "erp (history)"}


def test_loop_history_turns_zero_disables():
    reg = _Registry(["web.search", "web.fetch", "ask.user", "skill.load",
                     "note.set", "todos", "run.badge", "deliver.files",
                     "memory.search", "fs.list", "fs.read", "llm.call",
                     "tools.load", "erp_demo.search"])
    rt, _ = _runtime(reg, [_final("ok")])
    cfg = dict(CFG)
    cfg["tool_selection"] = {
        "mode": "auto",
        "keyword_namespaces": {"erp_demo": ["erp"]},
        "history_turns": 0,
    }
    rt.config = cfg
    rt.selector = ToolSelector(reg, cfg)
    asyncio.run(rt.run(
        "now their emails please",
        history=[{"role": "user", "content": "count the people in the erp"}]))
    assert rt.selector._diag["kw_triggered"] == {}


# ---- 2. phantom tool-call gate ----------------------------------------------


class _RecordingExec(_StubTool):
    """code.check stand-in that records whether it executed."""

    def __init__(self, name):
        super().__init__(name)
        self.executed = []

    async def execute(self, args, ctx):
        self.executed.append(args)
        return ToolResult(status="ok", result={"exit_code": 0})


def _phantom_rt(script, allowed):
    cc = _RecordingExec("code.check")
    reg = _Registry([], real={
        "code.check": cc,
        "erp_demo.search": _StubTool("erp_demo.search"),
        "tools.load": _StubTool("tools.load"),
    })
    rt, seen = _runtime(reg, script)
    return rt, seen, cc


def test_phantom_tool_call_refused_with_recovery():
    rt, seen, cc = _phantom_rt(
        [_tc("code.check", json.dumps(
            {"command": 'erp_demo.search(User, name="X")', "language": "bash"})),
         _final("recovered")],
        allowed=["code.check"])
    out = asyncio.run(rt.run("lookup a user", tools=["code.check"]))
    assert out["status"] == "ok" and out["answer"] == "recovered"
    assert cc.executed == []                      # never ran as shell code
    tool_msgs = [m.get("content", "") for msgs in seen for m in msgs
                 if m.get("role") == "tool"]
    assert any("tools.load" in c and "erp_demo" in c for c in tool_msgs)


def test_phantom_gate_passes_when_tool_is_allowed():
    rt, _, cc = _phantom_rt(
        [_tc("code.check", json.dumps(
            {"command": "erp_demo.search(User)", "language": "bash"})),
         _final("done")],
        allowed=None)
    out = asyncio.run(rt.run("x", tools=["code.check", "erp_demo.search"]))
    assert out["status"] == "ok"
    # The referenced tool IS in the allowlist — the call is the model's
    # business (here it still runs as code; the gate only guards the
    # known-but-not-loaded case).
    assert len(cc.executed) == 1


def test_phantom_gate_ignores_non_tool_dotted_calls():
    rt, _, cc = _phantom_rt(
        [_tc("code.check", json.dumps(
            {"command": "import json\nprint(json.load(open('x')))",
             "language": "python"})),
         _final("done")],
        allowed=None)
    out = asyncio.run(rt.run("parse a file", tools=["code.check"]))
    assert out["status"] == "ok"
    assert len(cc.executed) == 1                  # json.load is no JayNet tool
