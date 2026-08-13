"""Hard per-tool-call timeout: a blocking tool is cancelled so the run continues;
exempt tools (spawn orchestrators, long ops) run to completion."""
import asyncio
import time

from runtime.loop import AgentRuntime
from runtime.tool_base import ToolResult


class _Tool:
    def __init__(self, name, delay=0.0, private=False):
        self.name=name; self._delay=delay; self.private=private
    async def execute(self, args, ctx):
        if self._delay: await asyncio.sleep(self._delay)
        return ToolResult(status="ok", result={"ok": True}, tool_name=self.name)


class _Reg:
    def __init__(self, tools): self._t={t.name: t for t in tools}
    def get(self, n): return self._t.get(n)


class _Stub:
    _execute_tool = AgentRuntime._execute_tool
    _tool_call_timeout = AgentRuntime._tool_call_timeout
    def __init__(self, cfg, reg): self.config=cfg; self.registry=reg


CFG = {"tools": {"call_timeout_s": 0.2,
                 "call_timeout_overrides": {"slow.exempt": 0, "test.run": 600}}}


def test_timeout_resolution():
    s=_Stub(CFG, _Reg([]))
    assert s._tool_call_timeout("anything") == 0.2       # default
    assert s._tool_call_timeout("test.run") == 600.0     # override
    assert s._tool_call_timeout("slow.exempt") == 0.0    # exempt
    assert _Stub({"tools": {}}, _Reg([]))._tool_call_timeout("x") == 180.0  # unset default


def test_fast_tool_returns_normally():
    r=asyncio.run(_Stub(CFG, _Reg([_Tool("fast")]))._execute_tool("fast", {}, None))
    assert r.status == "ok"


def test_hanging_tool_is_cancelled():
    s=_Stub(CFG, _Reg([_Tool("hang", delay=5)]))         # 5s vs 0.2s limit
    t0=time.monotonic()
    r=asyncio.run(s._execute_tool("hang", {}, None))
    assert r.status == "error" and "timed out" in r.error
    assert time.monotonic()-t0 < 2                        # cancelled fast, not after 5s


def test_exempt_tool_runs_to_completion():
    # 0.5s delay exceeds the 0.2s default, but timeout=0 means no wrapper
    r=asyncio.run(_Stub(CFG, _Reg([_Tool("slow.exempt", delay=0.5)]))._execute_tool("slow.exempt", {}, None))
    assert r.status == "ok"


def test_unknown_tool():
    r=asyncio.run(_Stub(CFG, _Reg([]))._execute_tool("nope", {}, None))
    assert r.status == "error" and "unknown tool" in r.error
