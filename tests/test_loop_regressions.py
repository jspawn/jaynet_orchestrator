"""Loop regressions: rejected tool calls must not kill the run, a cancel
landing mid-sub-agent must propagate to the top level, and spawn tool-narrowing
must never widen into tools the parent didn't have. The real loop is driven
with a fake model (instance-level _model_turn) over a stub registry/trace —
no network, no LiteLLM."""
import asyncio
import json

from runtime.loop import AgentRuntime
from runtime.selector import ToolSelector
from tools.agent.spawn import AgentSpawn

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 8, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "privacy": {"remote_llm_tools": []},
}


class _StubTool:
    private = False

    def __init__(self, name):
        self.name = name

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}


class _Registry:
    def __init__(self, names, real=None):
        self._tools = {n: _StubTool(n) for n in names}
        self._tools.update(real or {})

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
    """A drivable AgentRuntime: real loop, fake model. `script` is the list of
    assistant messages the fake _model_turn returns in order. Returns
    (runtime, seen) — seen collects the message lists each model turn got."""
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
    rt._model_sems = {}
    rt._poll_safe = set()
    turns = list(script)
    seen = []

    async def fake_turn(messages, tools_schema, model=None, think=True, sampling=None):
        seen.append(messages)
        return {"message": turns.pop(0), "usage": {}}
    rt._model_turn = fake_turn
    return rt, seen


def _spawn_rt(script, child_run):
    """A runtime whose agent.spawn child runs are faked: depth-0 runs the real
    loop, anything deeper calls `child_run(task, **kwargs)` instead."""
    rt, seen = _runtime(
        _Registry(["agent.spawn", "fs.read", "fs.write"],
                  real={"agent.spawn": AgentSpawn()}), script)
    real_run = rt.run

    async def run_proxy(msg, **kw):
        if kw.get("depth", 0) > 0:
            return await child_run(msg, **kw)
        return await real_run(msg, **kw)
    rt.run = run_proxy
    return rt, seen


# ---- rejected tool calls: args stay None; the trajectory must not crash ----

def test_allowlist_rejected_call_keeps_run_alive():
    rt, _ = _runtime(_Registry(["fs.read", "fs.write"]),
                     [_tc("fs.read", "{}"), _final("recovered")])
    out = asyncio.run(rt.run("do a thing", tools=["fs.write"]))   # fs.read not allowed
    assert out["status"] == "ok" and out["answer"] == "recovered"
    assert "fs.read→error: tool 'fs.read' is not permitted in this run" in out["trajectory"]


def test_invalid_json_args_keeps_run_alive():
    rt, _ = _runtime(_Registry(["fs.read"]), [_tc("fs.read", "{not json"), _final("recovered")])
    out = asyncio.run(rt.run("do a thing"))
    assert out["status"] == "ok" and out["answer"] == "recovered"
    assert "invalid JSON args" in out["trajectory"]


# ---- cancellation: a cancel that lands mid-child must cancel the whole run ----

def test_cancel_during_child_propagates_to_top_level():
    async def child(msg, **kw):
        # Simulate the web /cancel arriving mid-child: the child run's own
        # handler swallows the CancelledError and returns a "cancelled" dict.
        asyncio.current_task().cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            return {"status": "cancelled", "answer": "", "run_id": "sub",
                    "budget": {"cost_usd": 0.25, "tokens": {"prompt": 10}}}
        raise AssertionError("cancel did not arrive")
    rt, _ = _spawn_rt([_tc("agent.spawn", json.dumps({"task": "subtask"})),
                       _final("parent continued — wrong")], child)
    out = asyncio.run(rt.run("delegate this"))
    assert out["status"] == "cancelled"
    # Budget reconciliation still ran before the cancellation propagated.
    assert out["budget"]["cost_usd"] == 0.25
    assert out["budget"]["tokens"]["prompt"] == 10


def test_top_level_cancel_still_clean():
    async def fake_turn(messages, tools_schema, model=None, think=True, sampling=None):
        asyncio.current_task().cancel()
        await asyncio.sleep(0)   # CancelledError raised here
    rt, _ = _runtime(_Registry([]), [])
    rt._model_turn = fake_turn
    out = asyncio.run(rt.run("hello"))
    assert out["status"] == "cancelled" and "cancelled" in out["error"]


def test_normal_child_completion_unaffected():
    seen_kw = {}

    async def child(msg, **kw):
        seen_kw.update(kw)
        return {"status": "ok", "answer": "child answer", "run_id": "sub",
                "budget": {"cost_usd": 0.10, "tokens": {"completion": 5}}}
    rt, _ = _spawn_rt([_tc("agent.spawn", json.dumps({"task": "subtask"})),
                       _final("parent done")], child)
    out = asyncio.run(rt.run("delegate this"))
    assert out["status"] == "ok" and out["answer"] == "parent done"
    assert seen_kw["depth"] == 1


# ---- spawn tool-narrowing: a child never gets tools the parent didn't have ----

def test_spawn_rejects_request_outside_parent_tools():
    calls = []

    async def child(msg, **kw):
        calls.append(kw)
        return {"status": "ok", "answer": "x", "run_id": "s", "budget": {}}
    rt, seen = _spawn_rt(
        [_tc("agent.spawn", json.dumps({"task": "t", "tools": ["fs.write"]})),
         _final("reported")], child)
    out = asyncio.run(rt.run("delegate", tools=["agent.spawn", "fs.read"]))
    assert out["status"] == "ok"
    assert calls == []                                   # the child never ran
    tool_msg = json.loads([m for m in seen[-1] if m.get("role") == "tool"][-1]["content"])
    assert tool_msg["status"] == "error" and "none of the requested tools" in tool_msg["error"]
    # The error names what IS permitted, so the model can retry sensibly.
    assert "agent.spawn" in tool_msg["error"] and "fs.read" in tool_msg["error"]


def test_spawn_no_request_inherits_parent_tools_and_disabled():
    captured = {}

    async def child(msg, **kw):
        captured.update(kw)
        return {"status": "ok", "answer": "x", "run_id": "s", "budget": {}}
    rt, _ = _spawn_rt([_tc("agent.spawn", json.dumps({"task": "t"})), _final()], child)
    out = asyncio.run(rt.run("delegate", tools=["agent.spawn", "fs.read"],
                             disabled_tools={"fs.write"}))
    assert out["status"] == "ok"
    assert captured["tools"] == ["agent.spawn", "fs.read"]   # exactly the parent's set
    assert captured["disabled_tools"] == {"fs.write"}        # propagated into the child


def test_spawn_partial_intersection_narrows_quietly():
    captured = {}

    async def child(msg, **kw):
        captured.update(kw)
        return {"status": "ok", "answer": "x", "run_id": "s", "budget": {}}
    rt, _ = _spawn_rt(
        [_tc("agent.spawn", json.dumps({"task": "t", "tools": ["fs.read", "fs.write"]})),
         _final()], child)
    out = asyncio.run(rt.run("delegate", tools=["agent.spawn", "fs.read"]))
    assert out["status"] == "ok"
    assert captured["tools"] == ["fs.read"]   # fs.write dropped, not granted


# ---- selector: an explicit list (even empty) is not "no preference" ----

def _sel(names, cfg=None):
    return ToolSelector(_Registry(names), cfg or {})


def test_select_explicit_empty_list_means_no_tools():
    assert _sel(["fs.read"]).select("anything", requested=[]) == []


def test_select_explicit_list_matching_nothing_is_not_all():
    assert _sel(["fs.read"]).select("anything", requested=["bogus.tool"]) == []


def test_select_none_keeps_all_mode_behavior():
    assert _sel(["fs.read"], {"tool_selection": {"mode": "all"}}).select("hi") is None


def test_select_none_keeps_auto_mode_behavior():
    s = _sel(["fs.read", "web.search"],
             {"tool_selection": {"mode": "auto",
                                 "keyword_namespaces": {"web": ["search"]}}})
    assert s.select("please search the web") == ["web.search"]


def test_select_auto_empty_still_falls_back_to_all():
    # Auto selecting nothing (trivial message, no core/keyword match) still
    # degrades to 'all' — only EXPLICIT lists stopped falling back.
    s = _sel(["fs.read"], {"tool_selection": {"mode": "auto"}})
    assert s.select("hi") is None


def test_select_explicit_still_respects_disabled():
    s = _sel(["fs.read", "fs.write"])
    assert s.select("x", requested=["fs"], disabled={"fs.write"}) == ["fs.read"]
