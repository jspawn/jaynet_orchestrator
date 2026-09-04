"""Strength gate (agent.strength_gate): when a request matches strength
keywords for a tag with a LIVE holder, inline implementation tools are
rejected until the first code.delegate — the routing nudge asks, the gate
enforces. Never fires without a live route. Real loop, fake model."""
import asyncio
import json

import tools.model.catalog as catalog
from runtime.tool_base import ToolResult
from tests.test_loop_regressions import _final, _Registry, _runtime, _tc

SECURITY_TASK = "Run a security audit: exploit the test app's sql injection."


class _ExecStub:
    """Executable stand-in: succeeds, mutates nothing on disk."""
    private = False

    def __init__(self, name):
        self.name = name

    def needs_confirmation(self, args, ctx): return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        return ToolResult(status="ok", result={"ok": True})


def _rt(script):
    reg = _Registry([], real={
        "fs.write": _ExecStub("fs.write"),
        "code.delegate": _ExecStub("code.delegate"),
    })
    return _runtime(reg, script)


def _patch_route(monkeypatch, alias):
    async def fake_route(config, wanted):
        return alias
    monkeypatch.setattr(catalog, "route_strength", fake_route)


def test_gate_rejects_inline_until_delegate(monkeypatch):
    _patch_route(monkeypatch, "dolphin-alias")
    script = [
        _tc("fs.write", json.dumps({"path": "exploit.py", "content": "x"})),
        _tc("code.delegate", json.dumps({"task": "write the exploit"})),
        _tc("fs.write", json.dumps({"path": "notes.txt", "content": "done"})),
        _final("delegated"),
    ]
    rt, seen = _rt(script)
    out = asyncio.run(rt.run(SECURITY_TASK))
    assert out["status"] == "ok"
    assert "this is security work" in out["trajectory"]
    # The trajectory caps errors at 80 chars — the full directive (strength
    # tag + live holder) rides the tool-result message the model saw.
    tool_results = [m.get("content", "") for msgs in seen for m in msgs
                    if m.get("role") == "tool"]
    assert any('strength=\\"security\\"' in c for c in tool_results)
    assert any("dolphin-alias" in c for c in tool_results)
    # After the delegate call the gate is disarmed: second write ran clean.
    assert "fs.write(notes.txt)→ok" in out["trajectory"]


def test_gate_silent_without_live_holder(monkeypatch):
    _patch_route(monkeypatch, None)
    script = [
        _tc("fs.write", json.dumps({"path": "exploit.py", "content": "x"})),
        _final("wrote it myself"),
    ]
    rt, _ = _rt(script)
    out = asyncio.run(rt.run(SECURITY_TASK))
    assert out["status"] == "ok"
    assert "this is security work" not in out["trajectory"]
    assert "fs.write(exploit.py)→ok" in out["trajectory"]


def test_gate_directive_names_the_swap(monkeypatch):
    """A stopped LOCAL preset carrying the tag arms the gate in swap mode —
    the directive tells the brain code.delegate swaps it in (no manual
    model.use, no silent allround fallback)."""
    async def fake_plan(config, wanted):
        return {"mode": "swap", "alias": "local-dolphin", "preset": "dolphin"}
    monkeypatch.setattr(catalog, "strength_route", fake_plan)
    script = [
        _tc("fs.write", json.dumps({"path": "exploit.py", "content": "x"})),
        _tc("code.delegate", json.dumps({"task": "write the exploit",
                                         "strength": "security"})),
        _final("delegated"),
    ]
    rt, seen = _rt(script)
    out = asyncio.run(rt.run(SECURITY_TASK))
    assert out["status"] == "ok"
    tool_results = [m.get("content", "") for msgs in seen for m in msgs
                    if m.get("role") == "tool"]
    assert any("swaps it onto its slot" in c for c in tool_results)
    assert any("local-dolphin" in c for c in tool_results)


def test_gate_injects_strength_when_the_brain_drops_it(monkeypatch):
    """Live evidence: 4/4 security delegates went out WITHOUT the strength=
    the directive asked for and silently routed coding (no swap). The gate
    knows the domain — it injects the tag into delegate args. An explicit
    strength= from the model still wins."""
    async def fake_plan(config, wanted):
        return {"mode": "swap", "alias": "local-dolphin", "preset": "dolphin"}
    monkeypatch.setattr(catalog, "strength_route", fake_plan)
    captured = {}

    class _Delegate(_ExecStub):
        async def execute(self, args, ctx):
            captured.update(args)
            return ToolResult(status="ok", result={"ok": True})

    reg = _Registry([], real={
        "fs.write": _ExecStub("fs.write"),
        "code.delegate": _Delegate("code.delegate"),
    })
    script = [
        _tc("code.delegate", json.dumps({"task": "write the exploit"})),
        _final("delegated"),
    ]
    rt, _ = _runtime(reg, script)
    out = asyncio.run(rt.run(SECURITY_TASK))
    assert out["status"] == "ok"
    assert captured.get("strength") == "security"


def test_gate_silent_on_unrelated_request(monkeypatch):
    _patch_route(monkeypatch, "dolphin-alias")
    script = [
        _tc("fs.write", json.dumps({"path": "list.txt", "content": "x"})),
        _final("done"),
    ]
    rt, _ = _rt(script)
    out = asyncio.run(rt.run("write my shopping list to a file"))
    assert out["status"] == "ok"
    assert "this is security work" not in out["trajectory"]


def test_gate_disabled_in_config(monkeypatch):
    _patch_route(monkeypatch, "dolphin-alias")
    script = [
        _tc("fs.write", json.dumps({"path": "exploit.py", "content": "x"})),
        _final("done"),
    ]
    rt, _ = _rt(script)
    rt.config["agent"] = {"strength_gate": {"enabled": False}}
    out = asyncio.run(rt.run(SECURITY_TASK))
    assert out["status"] == "ok"
    assert "this is security work" not in out["trajectory"]
