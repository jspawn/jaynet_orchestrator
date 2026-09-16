"""Brain coding-tool gate (tools.code.brain_mode: verify) + code.check policy.

The gate is the mechanical half of route-don't-do: with a coding specialist
present, the brain's frozen toolset loses code.run/code.execute/code.patch
and gains code.check, so implementation must go through code.delegate.
"""
import asyncio
from types import SimpleNamespace

from runtime.loop import _brain_code_gate, _coding_specialist_present
from runtime.tool_base import ToolContext
from tools.code.check import CodeCheck
from tools.code.run import CodeRun


class _Reg:
    def __init__(self, names):
        self._names = names

    def all(self):
        return [SimpleNamespace(name=n) for n in self._names]


TOOLS = ["code.run", "code.execute", "code.patch", "code.check", "fs.read",
         "web.fetch", "code.delegate"]


def _cfg(mode="verify", specialist=True):
    cfg = {"tools": {"code": {"brain_mode": mode}}}
    if specialist:
        cfg["models"] = {
            "slots": {"specialist": "coder"},
            "presets": {"coder": {"strengths": ["coding", "allround"]}}}
    return cfg


def test_gate_swaps_coding_tools_for_check():
    out = _brain_code_gate(_cfg(), _Reg(TOOLS), list(TOOLS), 0, set())
    assert "code.run" not in out and "code.execute" not in out
    assert "code.patch" not in out
    assert "code.check" in out
    assert "code.delegate" in out and "fs.read" in out   # unrelated untouched


def test_gate_materializes_none_toolset():
    # allowed=None means "all tools" — the gate must make the set explicit,
    # or the brain would keep code.run.
    out = _brain_code_gate(_cfg(), _Reg(TOOLS), None, 0, set())
    assert out is not None
    assert "code.run" not in out and "code.check" in out


def test_gate_inactive_without_specialist():
    out = _brain_code_gate(_cfg(specialist=False), _Reg(TOOLS),
                           list(TOOLS), 0, set())
    assert out == TOOLS                       # untouched passthrough


def test_gate_inactive_in_full_mode():
    out = _brain_code_gate(_cfg(mode="full"), _Reg(TOOLS), list(TOOLS),
                           0, set())
    assert out == TOOLS
    # code default is full when the key is missing entirely
    out = _brain_code_gate({"tools": {"code": {}}}, _Reg(TOOLS), list(TOOLS),
                           0, set())
    assert out == TOOLS


def test_gate_brain_only():
    out = _brain_code_gate(_cfg(), _Reg(TOOLS), list(TOOLS), 1, set())
    assert out == TOOLS                       # sub-agents keep everything


def test_gate_respects_disabled_and_missing_check():
    without_check = [t for t in TOOLS if t != "code.check"]
    out = _brain_code_gate(_cfg(), _Reg(TOOLS), without_check, 0,
                           {"code.check"})
    assert "code.check" not in out            # admin-disabled stays off
    out = _brain_code_gate(_cfg(), _Reg(without_check), list(without_check),
                           0, None)
    assert "code.run" not in out              # gated even without code.check


def test_specialist_detection_slots_and_tags():
    assert _coding_specialist_present(_cfg()) is True
    # specialist2/3 count too
    cfg = _cfg(specialist=False)
    cfg["models"] = {"slots": {"specialist2": "c"},
                     "presets": {"c": {"strengths": ["coding"]}}}
    assert _coding_specialist_present(cfg) is True
    # a specialist without the coding tag does not trigger the gate
    cfg["models"] = {"slots": {"specialist": "s"},
                     "presets": {"s": {"strengths": ["security"]}}}
    assert _coding_specialist_present(cfg) is False


def test_check_forces_verify_policy(monkeypatch, tmp_path):
    seen = {}

    async def fake_execute(self, args, ctx):
        seen.update(args)
        from runtime.tool_base import ToolResult
        return ToolResult(status="ok", result={"ok": True},
                          tool_name=self.name)

    monkeypatch.setattr(CodeRun, "execute", fake_execute)
    ctx = ToolContext(request_id="t", config={}, budget=None,
                      work_root=str(tmp_path))
    r = asyncio.run(CodeCheck().execute(
        {"command": "pytest -q", "network": True, "timeout_s": 600,
         "max_output_lines": 2000, "env": {"A": "1"}}, ctx))
    assert r.status == "ok" and r.tool_name == "code.check"
    assert seen["network"] is False
    assert seen["timeout_s"] == 120
    assert seen["max_output_lines"] == 200
    assert seen["env"] == {"A": "1"}          # env passes through (scrubbed later)
