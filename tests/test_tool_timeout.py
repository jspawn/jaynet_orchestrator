"""Hard per-tool-call timeout: a blocking tool is cancelled so the run continues;
exempt tools (spawn orchestrators, long ops) run to completion."""
import asyncio
import time
from pathlib import Path

import yaml

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


# ---- shipped override table completeness (config audit #27 C1) ----

SHIPPED = Path(__file__).resolve().parent.parent / "config" / "runtime.yaml"


def test_shipped_overrides_cover_internal_budgets():
    """Drift guard: a shipped tool whose OWN internal budget exceeds
    call_timeout_s must have an override entry above that budget — otherwise
    the wrapper kills the tool mid-work (live: llm.call vision/OCR and
    agent.fanout runs killed at 180s). Pinned via the known list (the audit's
    suggestion); extend it when a tool gains an internal budget."""
    cfg = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    tools = cfg["tools"]
    cap = float(tools["call_timeout_s"])
    overrides = tools["call_timeout_overrides"]
    # tool -> the tool's own internal worst-case budget (seconds)
    internal = {
        "llm.call": float((tools.get("llm") or {}).get("vision_timeout_s", 600)),
        "lint.run": float((tools.get("lint") or {}).get("timeout_s", 120)),
        # imagegen plugin server defaults (plugins/imagegen/server.py):
        # 300s cold-start health wait + 900s generation.
        "image.generate": 1200.0,
    }
    for name, budget in internal.items():
        if budget <= cap:
            continue
        assert name in overrides, (
            f"{name}'s own budget ({budget}s) exceeds call_timeout_s ({cap}s) "
            "with no call_timeout_overrides entry")
        assert overrides[name] == 0 or float(overrides[name]) > budget, (
            f"{name} override ({overrides[name]}s) is not above its own "
            f"budget ({budget}s)")


def test_shipped_orchestrators_stay_untimed():
    """Sub-agent orchestrators are bounded by the BUDGET via their children;
    a wrapper would kill them mid-run — they must keep a 0 override."""
    cfg = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    overrides = cfg["tools"]["call_timeout_overrides"]
    for name in ("architect", "agent.spawn", "agent.fanout", "chain.run"):
        assert overrides.get(name) == 0, f"{name} lost its 0 override"
