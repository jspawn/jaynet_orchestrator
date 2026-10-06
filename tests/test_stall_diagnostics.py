"""Stall ladder vs diagnostics (delta-fail investigation 2026-10-06,
tb-regex-log): six code.check diagnostic turns with DIFFERENT args and
FRESH results found the root cause — then the armed stall hard-stop
blocked the fs.write carrying the diagnosed fix, and wrap_up forced a
final answer stating a fix it wasn't allowed to apply. Fixed two ways:
(a) a code.check-only turn whose calls are all NEW (args not repeated,
result different from the immediately previous check result) is NEUTRAL
on the ladder — identical-repeat check turns still escalate (the "14
identical runs" case the no-product rule was built for); (b) while the
stop is armed, fs.write/fs.edit pass the dispatch gate — a real mutation
disarms via the ladder's own reset. Real loop, fake model (convention:
the scaffolding helpers are copied from test_loop_regressions, not
shared)."""
import asyncio
import json
import tempfile

from runtime.loop import AgentRuntime
from runtime.selector import ToolSelector
from runtime.tool_base import ToolResult

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 40, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "privacy": {"remote_llm_tools": []},
}


class _Stub:
    """Exec-logging stub; read_only stubs never bump the mutation
    generation (a no-progress turn), mutating ones do (real progress —
    the ladder's own reset signal)."""
    private = False

    def __init__(self, name, log, read_only=True, varying=False):
        self.name = name
        self.read_only = read_only
        self._log = log
        self._varying = varying

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        self._log.append(self.name)
        text = f"result for {args}" if self._varying else "same result"
        return ToolResult(status="ok", tool_name=self.name,
                          result={"text": text})


class _Registry:
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}

    def all(self):
        return list(self._tools.values())

    def get(self, name):
        return self._tools.get(name)

    def openai_schemas(self, allowed=None):
        return [t.to_openai_schema() for n, t in self._tools.items()
                if allowed is None or n in allowed]


class _Trace:
    def start_run(self, *a, **k): pass
    def log(self, *a, **k): pass
    def finish_run(self, *a, **k): pass


def _tc(name, arguments):
    """One assistant message carrying a single tool call."""
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": name, "arguments": arguments}}]}


def _final(text="done"):
    return {"role": "assistant", "content": text}


def _runtime(registry, script):
    """A drivable AgentRuntime: real loop, fake model. Returns (runtime,
    seen) — seen collects the message lists each model turn got."""
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.config = dict(CFG)
    rt.registry = registry
    rt.selector = ToolSelector(registry, rt.config)
    rt.trace = _Trace()
    rt.system_prompt = "test"
    rt.skill_catalog = ""
    rt.litellm_base = "http://x:4000"
    rt.model = "local-orchestrator"
    rt.cost_table = {}
    rt.brain_info = {}
    rt.vision_enabled = False
    rt._local_concurrency = {}
    rt._local_aliases = frozenset()
    rt._model_sems = {}
    rt._poll_safe = set()
    turns = list(script)
    seen = []

    async def fake_turn(messages, tools_schema, model=None, think=True, sampling=None):
        seen.append(messages)
        return {"message": turns.pop(0), "usage": {}}
    rt._model_turn = fake_turn
    return rt, seen


def _run(log, tools, script):
    reg = _Registry(tools)
    rt, seen = _runtime(reg, script)
    events = []

    async def on_event(ev):
        events.append(ev)
    out = asyncio.run(rt.run("diagnose and fix",
                             work_root=tempfile.mkdtemp(), on_event=on_event))
    return out, events, seen


def _checks(n, start=0):
    """n code.check turns with DISTINCT args (the duplicate guard counts
    exact repeats — vary them)."""
    return [_tc("code.check", json.dumps({"command": f"probe {i}"}))
            for i in range(start, start + n)]


def test_fresh_diagnostics_do_not_escalate():
    """The tb-regex-log shape: code.check-only turns, distinct args AND a
    fresh result every time — diagnostic progress the mutation generation
    can't see. The ladder must not move: no rung events, no hard stop."""
    log = []
    tools = [_Stub("code.check", log, read_only=False, varying=True),
             _Stub("fs.write", log, read_only=False)]
    out, events, seen = _run(log, tools, _checks(7) + [_final("root cause found")])
    assert out["status"] == "ok"
    assert log == ["code.check"] * 7               # nothing refused
    assert not any(e["type"] == "stall_check" for e in events)
    assert not any(e["type"] == "stall_hard_stop" for e in events)


def test_identical_result_check_turns_still_escalate():
    """The case the no-product rule was built for: check turns whose
    results are the SAME as the previous one (even with distinct args —
    nothing learned) still count as no-progress and arm the hard stop."""
    log = []
    tools = [_Stub("code.check", log, read_only=False, varying=False)]
    out, events, seen = _run(log, tools, _checks(7) + [_final("gave up")])
    assert out["status"] == "ok"
    assert any(e["type"] == "stall_hard_stop" and e["data"].get("armed")
               for e in events)


def test_armed_stop_write_passes_and_disarms():
    """Stop armed by no-progress reads: the fs.write carrying the fix goes
    THROUGH the dispatch gate, the mutation disarms the stop via the
    ladder's own reset, and work tools execute again afterwards."""
    log = []
    tools = [_Stub("x.read", log, read_only=True),
             _Stub("fs.write", log, read_only=False)]
    reads = [_tc("x.read", json.dumps({"n": i})) for i in range(6)]
    script = (reads                                # 6 no-progress → armed
              + [_tc("fs.write", '{"path": "fix.py", "content": "fixed"}'),
                 _tc("x.read", '{"n": 6}'),        # disarmed → executes
                 _final("fixed")])
    out, events, seen = _run(log, tools, script)
    assert out["status"] == "ok"
    assert any(e["type"] == "stall_hard_stop" and e["data"].get("armed")
               for e in events)
    assert "fs.write" in log, "the fix write must pass the armed gate"
    assert log.count("x.read") == 7, "work tools run again after the write"


def test_armed_stop_code_check_stays_blocked():
    """The write pass-through doesn't reopen verify-spin: code.check is
    still refused while the stop is armed (an armed stop exists to close
    verify-spin loops, not to feed them)."""
    log = []
    tools = [_Stub("x.read", log, read_only=True),
             _Stub("code.check", log, read_only=False)]
    reads = [_tc("x.read", json.dumps({"n": i})) for i in range(6)]
    script = (reads
              + [_tc("code.check", '{"command": "probe again"}'),
                 _final("gave up")])
    out, events, seen = _run(log, tools, script)
    assert out["status"] == "ok"
    assert any(e["type"] == "stall_hard_stop" and e["data"].get("armed")
               for e in events)
    assert "code.check" not in log, "verify-spin stays closed while armed"
    tool_txt = [m.get("content") or "" for call in seen for m in call]
    assert any("stall hard-stop" in c or "stall_hard_stop" in c
               for c in tool_txt)
