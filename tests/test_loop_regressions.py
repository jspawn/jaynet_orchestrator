"""Loop regressions: rejected tool calls must not kill the run, a cancel
landing mid-sub-agent must propagate to the top level, and spawn tool-narrowing
must never widen into tools the parent didn't have. The real loop is driven
with a fake model (instance-level _model_turn) over a stub registry/trace —
no network, no LiteLLM."""
import asyncio
import gc
import json
import re
import tempfile
from pathlib import Path

import httpx

from runtime.budget import Budget
from runtime.loop import AgentRuntime, _child_budget, _strip_think
from runtime.quick_reply import QuickReply, _display_name
from runtime.selector import ToolSelector
from runtime.tool_base import ToolResult
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


def test_invalid_json_args_sanitized_in_history():
    """The broken args string must not be re-sent: llama-server 500s parsing
    HISTORY tool calls with invalid-JSON arguments (live: tb-mcmc-sampling-stan
    died on turn 3). The loop replaces them with valid empty JSON in place."""
    rt, seen = _runtime(_Registry(["fs.read"]),
                        [_tc("fs.read", "{not json"), _final("recovered")])
    out = asyncio.run(rt.run("do a thing"))
    assert out["status"] == "ok"
    # The second model turn's message list holds the first assistant message:
    # its tool-call arguments must be valid JSON now, not "{not json".
    assistant = next(m for m in seen[-1]
                     if m.get("role") == "assistant" and m.get("tool_calls"))
    args = assistant["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args) == {}


def test_malformed_tool_call_entry_keeps_run_alive():
    # No "function" payload and no "id" at all — must degrade to an error
    # tool-result fed back to the model, not an [Internal error] run death.
    bad = {"role": "assistant", "content": None, "tool_calls": [{"type": "function"}]}
    rt, _ = _runtime(_Registry(["fs.read"]), [bad, _final("recovered")])
    out = asyncio.run(rt.run("do a thing"))
    assert out["status"] == "ok" and out["answer"] == "recovered"
    assert "malformed tool call" in out["trajectory"]


def test_non_dict_tool_args_keep_run_alive():
    # Valid JSON, but a list — tool args must be an object; error result, no crash.
    rt, _ = _runtime(_Registry(["fs.read"]), [_tc("fs.read", "[1, 2]"), _final("recovered")])
    out = asyncio.run(rt.run("do a thing"))
    assert out["status"] == "ok" and out["answer"] == "recovered"
    assert "must be a JSON object" in out["trajectory"]


# ---- spawn budget exhaustion: an enabled-but-spent ceiling refuses the spawn ----

def test_spawn_refused_when_parent_token_ceiling_spent():
    calls = []

    async def child(msg, **kw):
        calls.append(kw)
        return {"status": "ok", "answer": "x", "run_id": "s", "budget": {}}
    rt, _ = _spawn_rt([_tc("agent.spawn", json.dumps({"task": "t"})),
                       _final("wrapped up")], child)
    # Token ceiling ENABLED but tiny; the first model turn's reported usage
    # spends it, so the same-turn spawn computes a remaining allowance of 0.
    rt.config = dict(CFG, budgets={"max_iterations": 8, "max_wall_clock_s": 60.0,
                                   "max_cost_usd": 0.0, "max_total_tokens": 10})
    base_turn = rt._model_turn

    async def usage_turn(messages, tools_schema, model=None, think=True, sampling=None):
        out = await base_turn(messages, tools_schema, model=model, think=think,
                              sampling=sampling)
        out["usage"] = {"prompt_tokens": 5000, "completion_tokens": 10}
        return out
    rt._model_turn = usage_turn
    out = asyncio.run(rt.run("delegate"))
    assert calls == []                                  # the child never ran
    assert "token budget is exhausted" in out["trajectory"]
    # The parent's own (enabled) ceiling then trips on the next tick.
    assert out["status"] == "budget_exceeded"


def test_spawn_with_disabled_parent_dims_inherits_unlimited():
    captured = {}

    async def child(msg, **kw):
        captured.update(kw)
        return {"status": "ok", "answer": "x", "run_id": "s",
                "budget": {"cost_usd": 0.0, "tokens": {}}}
    rt, _ = _spawn_rt([_tc("agent.spawn", json.dumps({"task": "t"})), _final()], child)
    # 0 ceilings = DISABLED (Budget.check reads 0 as "no ceiling"): the child
    # legitimately inherits "unlimited" rather than being refused.
    rt.config = dict(CFG, budgets={"max_iterations": 8, "max_wall_clock_s": 60.0,
                                   "max_cost_usd": 0.0, "max_total_tokens": 0})
    out = asyncio.run(rt.run("delegate"))
    assert out["status"] == "ok"
    assert captured["budget_overrides"]["max_cost_usd"] == 0.0
    assert captured["budget_overrides"]["max_total_tokens"] == 0


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


# ---- loop: run_overrides["force_tools"] joins the frozen selection ----
# Plugin-declared project tools (web layer fires the project_tools hook) must
# be reachable even when no keyword selects their namespace — same shape as
# the /goal force-add, but honoring the admin's disabled list.

_FORCE_CFG = {"mode": "auto", "core_namespaces": ["llm"],
              "keyword_namespaces": {}}


def _force_run(force, disabled=()):
    reg = _Registry(["web.search", "llm.call", "graph.build", "graph.query"])
    rt, _ = _runtime(reg, [_final("ok")])
    # ToolSelector snapshots its config at construction — set THEN rebuild.
    rt.config["tool_selection"] = dict(_FORCE_CFG)
    rt.selector = ToolSelector(reg, rt.config)
    events = []

    async def on_event(ev):
        events.append(ev)

    asyncio.run(rt.run("how does auth connect to the db?",
                       run_overrides={"force_tools": force},
                       disabled_tools=set(disabled),
                       on_event=on_event))
    sel = next(e for e in events if e["type"] == "tool_selection")
    return sel["data"]["selected"]


def test_force_tools_appended_and_unknown_dropped():
    selected = _force_run(["graph.build", "graph.query", "graph.bogus"])
    assert "graph.build" in selected
    assert "graph.query" in selected
    assert "graph.bogus" not in selected


def test_force_tools_respects_disabled():
    selected = _force_run(["graph.build", "graph.query"],
                          disabled={"graph.build"})
    assert "graph.build" not in selected
    assert "graph.query" in selected


def test_force_tools_noop_when_selector_returns_all():
    # mode 'all' (select() -> None) already exposes everything — the list
    # must not crash the run or change the 'all' verdict.
    reg = _Registry(["web.search", "graph.build"])
    rt, _ = _runtime(reg, [_final("ok")])
    events = []

    async def on_event(ev):
        events.append(ev)

    asyncio.run(rt.run("hi", run_overrides={"force_tools": ["graph.build"]},
                       on_event=on_event))
    sel = next(e for e in events if e["type"] == "tool_selection")
    assert sel["data"]["selected"] == "all"


# ---- selector: full_toolset_keywords expose everything (selftest) ----

def test_select_full_toolset_keyword_returns_all():
    names = ["fs.read", "fs.write", "web.search", "rag.index"]
    s = _sel(names, {"tool_selection": {"mode": "auto",
                                        "core_namespaces": ["fs.read"],
                                        "full_toolset_keywords": ["selftest"]}})
    assert s.select("run a selftest of the tools") == names


def test_select_full_toolset_keyword_respects_disabled():
    s = _sel(["fs.read", "fs.write"],
             {"tool_selection": {"mode": "auto",
                                 "full_toolset_keywords": ["selftest"]}})
    assert s.select("selftest please", disabled={"fs.write"}) == ["fs.read"]


def test_select_full_toolset_keyword_loses_to_explicit_list():
    s = _sel(["fs.read", "fs.write"],
             {"tool_selection": {"mode": "auto",
                                 "full_toolset_keywords": ["selftest"]}})
    assert s.select("selftest", requested=["fs.read"]) == ["fs.read"]


def test_select_full_toolset_keyword_not_in_all_mode():
    # 'all' mode already returns everything; the keyword must not break it.
    s = _sel(["fs.read"], {"tool_selection": {"mode": "all",
                                              "full_toolset_keywords": ["selftest"]}})
    assert s.select("selftest") is None


# ---- selector: category aliases (the gate-prompt / tools.load vocabulary) ----

_NAMES = ["code.execute", "code.patch", "lint.run", "test.run", "architect",
          "fs.read", "fs.write", "archives.create", "pdf.create",
          "verify.score", "trace.query", "chain.run", "mcp.call",
          "web.search", "git.status"]


def test_expand_category_alias_coding():
    s = _sel(_NAMES)
    assert s.select("x", requested=["coding"]) == [
        "code.execute", "code.patch", "lint.run", "test.run", "architect"]


def test_expand_category_alias_verification():
    s = _sel(_NAMES)
    assert s.select("x", requested=["verification"]) == ["verify.score", "trace.query"]


def test_expand_category_alias_integration():
    s = _sel(_NAMES)
    assert s.select("x", requested=["integration"]) == ["chain.run", "mcp.call"]


def test_expand_alias_mixed_with_exact_names_and_namespaces():
    s = _sel(_NAMES)
    assert s.select("x", requested=["files", "git", "web.search"]) == [
        "fs.read", "fs.write", "archives.create", "pdf.create",
        "web.search", "git.status"]


def test_expand_unknown_category_still_resolves_nothing():
    s = _sel(_NAMES)
    assert s.select("x", requested=["bogus-category"]) == []


# ---- stall watchdog + turn timeout: a hung turn ends the run as "stalled" ----

class _SilentStream:
    """A streaming response that never emits a line (zombie backend)."""
    status_code = 200

    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False

    async def aiter_lines(self):
        await asyncio.sleep(3600)       # silence, forever
        yield ""                        # pragma: no cover — never reached


class _SilentClient:
    def __init__(self, timeout=None): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    def stream(self, *a, **k): return _SilentStream()


class _DripStream:
    """Emits a content chunk every 10ms forever — alive, but never finishes."""
    status_code = 200

    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False

    async def aiter_lines(self):
        while True:
            await asyncio.sleep(0.01)
            yield 'data: {"choices":[{"delta":{"content":"x"}}]}'


class _DripClient:
    def __init__(self, timeout=None): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    def stream(self, *a, **k): return _DripStream()


class _SeqStream:
    """Plays back a fixed list of SSE lines, then ends the stream."""
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _SeqClient:
    scripts: list = []

    def __init__(self, timeout=None): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    def stream(self, *a, **k): return _SeqStream(_SeqClient.scripts.pop(0))


class _TimeoutClient:
    """Every POST times out (httpx-side), as when a non-streaming turn overruns."""
    def __init__(self, timeout=None): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False

    async def post(self, *a, **k):
        raise httpx.TimeoutException("read timed out")


def _cfg(rt, **budget_over):
    """Swap in a fresh config (the shared CFG dicts must never be mutated)."""
    orch = dict(CFG["orchestrator"])
    budgets = dict(CFG["budgets"])
    orch.update(budget_over.pop("orchestrator", {}))
    budgets.update(budget_over.pop("budgets", {}))
    rt.config = {**CFG, "orchestrator": orch, "budgets": budgets}
    return rt


def test_silent_stream_trips_stall_watchdog_run_ends_stalled(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "k")
    monkeypatch.setattr("runtime.model_client.httpx.AsyncClient", _SilentClient)
    rt, _ = _runtime(_Registry([]), [])
    _cfg(rt, budgets={"stall_s": 0.05})
    out = asyncio.run(rt.run("hi", stream=True))
    assert out["status"] == "stalled"
    assert "no streamed output" in out["error"]        # silence, not a timeout
    assert "[Run terminated: model stalled]" in out["answer"]
    assert "Partial result" in out["answer"]


def test_streaming_total_turn_timeout_ends_run_stalled(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "k")
    monkeypatch.setattr("runtime.model_client.httpx.AsyncClient", _DripClient)
    rt, _ = _runtime(_Registry([]), [])
    _cfg(rt, orchestrator={"turn_timeout_s": 0.05}, budgets={"stall_s": 60})
    out = asyncio.run(rt.run("hi", stream=True))
    assert out["status"] == "stalled"
    assert "total turn timeout" in out["error"]        # timeout, not silence


def test_turn_timeout_ends_run_stalled_not_internal_error(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "k")
    monkeypatch.setattr("runtime.model_client.httpx.AsyncClient", _TimeoutClient)
    rt, _ = _runtime(_Registry([]), [])
    del rt._model_turn                                 # use the REAL non-streaming turn
    _cfg(rt, orchestrator={"turn_timeout_s": 7})
    out = asyncio.run(rt.run("hi"))                    # stream=False path
    assert out["status"] == "stalled"
    assert "total turn timeout" in out["error"]
    assert "Internal error" not in out["answer"]


def test_stall_watchdog_never_fires_during_tool_execution(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "k")
    monkeypatch.setattr("runtime.model_client.httpx.AsyncClient", _SeqClient)
    _SeqClient.scripts = [
        ['data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"slow.tool","arguments":"{}"}}]}}]}',
         "data: [DONE]"],
        ['data: {"choices":[{"delta":{"content":"survived"}}]}',
         "data: [DONE]"],
    ]

    class _SlowTool(_StubTool):
        async def execute(self, args, ctx):
            await asyncio.sleep(0.2)                   # silent stretch >> stall_s
            return ToolResult(status="ok", result="done")

    rt, _ = _runtime(_Registry([], real={"slow.tool": _SlowTool("slow.tool")}), [])
    _cfg(rt, budgets={"stall_s": 0.05})
    out = asyncio.run(rt.run("run the slow tool", stream=True))
    assert out["status"] == "ok" and out["answer"] == "survived"


def test_turn_timeout_plumbed_to_both_turn_paths(monkeypatch):
    seen = []

    class _RecStream:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def aiter_lines(self):
            yield "data: [DONE]"

    class _RecClient:
        def __init__(self, *a, **k): pass   # shared client: built once, no timeout
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

        async def post(self, *a, **k):
            seen.append(k.get("timeout"))   # per-request timeout
            class R:
                status_code = 200
                def json(self):
                    return {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                            "usage": {}}
            return R()

        def stream(self, *a, **k):
            seen.append(k.get("timeout"))   # per-request timeout
            return _RecStream()

    monkeypatch.setenv("LITELLM_MASTER_KEY", "k")
    monkeypatch.setattr("runtime.model_client.httpx.AsyncClient", _RecClient)
    rt, _ = _runtime(_Registry([]), [])
    del rt._model_turn                                 # real non-streaming turn
    _cfg(rt, orchestrator={"turn_timeout_s": 321})
    asyncio.run(rt._model_turn([], []))
    asyncio.run(rt._model_turn_streaming([], [], None))
    assert seen == [321, 321]


def test_turn_timeout_and_stall_defaults():
    rt, _ = _runtime(_Registry([]), [])
    assert rt._turn_timeout_s() == 900.0
    assert rt._stall_s() == 180.0


# ---- wall-clock 0 = no ceiling (local-first budget posture) ----

def test_wall_clock_zero_means_no_ceiling():
    b = Budget(max_iterations=100, max_wall_clock_s=0,
               max_cost_usd=1.0, max_total_tokens=1000)
    for _ in range(3):
        b.tick()                                       # must not raise
    b.started_at -= 3600                               # an hour in: still no ceiling
    b.check()
    frac, _ = b.pressure()
    assert frac < 1.0                                  # time dim contributes nothing


def test_run_completes_with_wall_clock_disabled():
    rt, _ = _runtime(_Registry([]), [_final("all good")])
    _cfg(rt, budgets={"max_wall_clock_s": 0})
    out = asyncio.run(rt.run("hello"))
    assert out["status"] == "ok" and out["answer"] == "all good"


def test_child_budget_wall_unclamped_when_parent_wall_disabled():
    b = _child_budget({}, {}, 8, 1.0, 1000, 0.0)
    assert b["max_wall_clock_s"] == 0.0                # inherits "no ceiling"
    b = _child_budget({"max_wall_clock_s": 120}, {}, 8, 1.0, 1000, 0.0)
    assert b["max_wall_clock_s"] == 120.0              # explicit wall kept
    b = _child_budget({"max_wall_clock_s": 900}, {}, 8, 1.0, 1000, 300.0)
    assert b["max_wall_clock_s"] == 300.0              # bounded parent still clamps


def test_spawn_with_disabled_parent_wall_gets_disabled_wall():
    captured = {}

    async def child(msg, **kw):
        captured.update(kw)
        return {"status": "ok", "answer": "x", "run_id": "s", "budget": {}}
    rt, _ = _spawn_rt([_tc("agent.spawn", json.dumps({"task": "t"})), _final()], child)
    _cfg(rt, budgets={"max_wall_clock_s": 0})
    out = asyncio.run(rt.run("delegate"))
    assert out["status"] == "ok"
    assert captured["budget_overrides"]["max_wall_clock_s"] == 0.0


# ---- small fixes ----

def test_strip_think_unterminated_block_removed():
    assert _strip_think("answer<think>unfinished chain") == "answer"
    assert _strip_think("<think>only chain") == ""
    assert _strip_think("a<think>x</think>b") == "ab"               # closed pair
    assert _strip_think("a<think>x</think>b<think>tail") == "ab"    # mixed
    assert _strip_think("plain") == "plain"


def test_tmp_dir_cleaned_when_setup_raises():
    # An exception between tmp-dir creation and the loop's main try (here: the
    # tool selector blowing up) must not leak the per-run scratch dir.
    rt, _ = _runtime(_Registry([]), [])

    class _BoomSelector:
        mode = "all"
        def select(self, *a, **k):
            raise RuntimeError("boom in setup")
    rt.selector = _BoomSelector()
    rid = "deadbeef-cleanup-test"
    tmp = Path(tempfile.gettempdir())
    before = set(tmp.glob(f"orchrun-{rid[:8]}-*"))
    try:
        asyncio.run(rt.run("hi", run_id=rid))
    except RuntimeError:
        pass
    gc.collect()                                       # drop frames -> finalizer runs
    leaked = [p for p in tmp.glob(f"orchrun-{rid[:8]}-*") if p not in before]
    assert leaked == []


def test_truncated_tool_result_is_valid_json():
    big = ToolResult(status="ok", result={"blob": "x" * 50000})
    msg = big.to_model_message()
    parsed = json.loads(msg)                           # the old splice didn't parse
    assert parsed["status"] == "ok" and parsed["__truncated__"] is True
    assert len(msg) < 21000


def test_select_all_mode_applies_disabled():
    # 'all' + disabled must materialize the filtered list — None would mean
    # "every tool" downstream and silently expose the disabled ones.
    s = _sel(["fs.read", "fs.write"], {"tool_selection": {"mode": "all"}})
    assert s.select("hi", disabled={"fs.write"}) == ["fs.read"]


def test_select_static_mode_without_list_applies_disabled():
    s = _sel(["fs.read", "fs.write"], {"tool_selection": {"mode": "static"}})
    assert s.select("hi", disabled={"fs.write"}) == ["fs.read"]
    # ... while no disabled set keeps the old "None = all" behaviour.
    assert _sel(["fs.read"], {"tool_selection": {"mode": "static"}}).select("hi") is None


def test_display_name_whitespace_only_handle_no_crash():
    assert _display_name("___") == "___"               # raw fallback, no IndexError
    assert _display_name("john_doe") == "John"         # normal path unchanged
    qr = QuickReply.__new__(QuickReply)
    qr.rules = [(re.compile(r"^hi$", re.IGNORECASE), ["Hi {name}!"])]
    assert qr.match("hi", "___") == "Hi ___!"


# ---- batch 5: cache-friendly prefix, context-pressure guard, liveness ----

def test_datetime_rides_as_note_before_user_message():
    # The datetime is the only per-run-varying fragment; it must NOT live in
    # the system prompt (it would break the server prompt cache for everything
    # rendered after it — the whole replayed history). It travels as its own
    # one-line system message right before the user turn.
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    rt.skill_catalog = "SKILLCATALOG"
    asyncio.run(rt.run("hi", extra_system="EXTRASYS",
                       work_root="/tmp/wk"))
    msgs = seen[0]
    sysmsg = msgs[0]["content"]
    assert "Current date/time" not in sysmsg
    for marker in ("SKILLCATALOG", "EXTRASYS", "Your workspace"):
        assert marker in sysmsg, marker
    # Env-manager doctrine: the model must never improvise `.venv/bin/pip`
    # (uv-managed venvs have none) — the prompt itself teaches the uv way.
    assert "uv pip install --python" in sysmsg
    assert ".venv/bin/pip" in sysmsg          # named as the thing NOT to do
    uidx = next(i for i, m in enumerate(msgs)
                if m == {"role": "user", "content": "hi"})
    note = msgs[uidx - 1]
    assert note["role"] == "system"
    assert "Current date/time" in note["content"]


def test_datetime_note_stresses_current_year():
    # Flagged run: the model searched for last year's prices from stale
    # training data. The note must name the current year explicitly.
    import datetime as _dtm
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    asyncio.run(rt.run("hi"))
    note = [m for m in seen[0]
            if m["role"] == "system" and "Current date/time" in m["content"]]
    assert len(note) == 1
    assert "training data is OLDER" in note[0]["content"]
    assert f"current year ({_dtm.datetime.now().year})" in note[0]["content"]


def test_location_config_injected_in_system_prompt():
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    _cfg(rt, orchestrator={"location": "Zürich, Switzerland"})
    asyncio.run(rt.run("hi"))
    sysmsg = seen[0][0]["content"]
    assert "User location: Zürich, Switzerland" in sysmsg
    assert "assume this for local, travel" in sysmsg


def test_location_unset_tells_model_to_ask():
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    asyncio.run(rt.run("hi"))
    sysmsg = seen[0][0]["content"]
    assert "User location: unknown" in sysmsg
    assert "ask the user before searching" in sysmsg


def test_run_overrides_timezone_beats_config():
    # The account-page timezone (per user, server-side) reaches the loop via
    # run_overrides and must win over orchestrator.timezone from runtime.yaml.
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    _cfg(rt, orchestrator={"timezone": "UTC"})

    def note(msgs):
        return next(m["content"] for m in msgs
                    if m["role"] == "system" and "Current date/time" in m["content"])

    asyncio.run(rt.run("hi", run_overrides={"timezone": "Asia/Tokyo"}))
    assert "JST" in note(seen[0])
    seen.clear()
    asyncio.run(rt.run("hi", run_overrides={"timezone": None}))
    assert "UTC" in note(seen[0])


def test_context_pressure_injects_one_wrap_up_nudge():
    rt, seen = _runtime(_Registry(["fs.read"]), [])
    _cfg(rt, orchestrator={"context_tokens": 1000})
    turns = [_tc("fs.read", "{}"), _tc("fs.read", "{}"), _final("wrapped")]

    async def fake(messages, tools_schema, model=None, think=True, sampling=None):
        seen.append(messages)
        return {"message": turns.pop(0),
                "usage": {"prompt_tokens": 850, "completion_tokens": 10}}  # 85% full
    rt._model_turn = fake
    out = asyncio.run(rt.run("work"))
    assert out["status"] == "ok"
    for turn_msgs in (seen[1], seen[2]):           # fired before turn 2 …
        nudges = [m for m in turn_msgs if m.get("role") == "system"
                  and "CONTEXT NOTICE" in (m.get("content") or "")]
        assert len(nudges) == 1                    # … and exactly once (one-shot)


def test_empty_capped_turn_gets_one_nudge():
    """A turn cut at the completion cap DURING REASONING (finish 'length',
    empty content — thinking ate the whole budget; tb-regex-log ended 'ok'
    with an empty answer after 8192 pure-thinking tokens) gets ONE
    brief-reply nudge instead of an empty final answer."""
    rt, seen = _runtime(_Registry([]), [])
    turns = [({"role": "assistant", "content": None}, "length"),
             ({"role": "assistant", "content": "the answer"}, "stop")]

    async def fake(messages, tools_schema, model=None, think=True, sampling=None):
        seen.append(messages)
        m, fr = turns.pop(0)
        return {"message": m, "usage": {"completion_tokens": 8192},
                "finish_reason": fr}
    rt._model_turn = fake
    out = asyncio.run(rt.run("q"))
    assert out["status"] == "ok" and out["answer"] == "the answer"
    nudges = [m for m in seen[1]
              if "cut off at the completion-token cap" in (m.get("content") or "")]
    assert len(nudges) == 1


def test_empty_capped_turn_nudges_only_once():
    """If the model returns empty-at-cap AGAIN after the nudge, the run ends
    rather than nudging forever."""
    rt, seen = _runtime(_Registry([]), [])
    turns = [({"role": "assistant", "content": None}, "length"),
             ({"role": "assistant", "content": None}, "length")]

    async def fake(messages, tools_schema, model=None, think=True, sampling=None):
        seen.append(messages)
        m, fr = turns.pop(0)
        return {"message": m, "usage": {}, "finish_reason": fr}
    rt._model_turn = fake
    out = asyncio.run(rt.run("q"))
    assert out["answer"] == ""
    # The nudge went out after turn 1 …
    assert any("cut off at the completion-token cap" in (m.get("content") or "")
               for m in seen[1])
    # … but turn 2's empty-at-cap reply ended the run — no third turn.
    assert len(seen) == 2


def test_context_guard_disabled_without_context_tokens():
    rt, seen = _runtime(_Registry(["fs.read"]), [])
    _cfg(rt, budgets={"max_total_tokens": 10**12})   # keep the huge usage affordable
    turns = [_tc("fs.read", "{}"), _final("done")]

    async def fake(messages, tools_schema, model=None, think=True, sampling=None):
        seen.append(messages)
        return {"message": turns.pop(0),
                "usage": {"prompt_tokens": 10**9, "completion_tokens": 10}}
    rt._model_turn = fake
    out = asyncio.run(rt.run("work"))
    assert out["status"] == "ok"
    assert all("CONTEXT NOTICE" not in (m.get("content") or "")
               for m in seen[1])


class _KeepAliveStream:
    """Keepalive comments + role-only deltas every 10ms: alive on the wire,
    never generating content — the zombie-behind-a-chatty-proxy case."""
    status_code = 200

    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False

    async def aiter_lines(self):
        while True:
            await asyncio.sleep(0.01)
            yield ": keep-alive"
            yield 'data: {"choices":[{"delta":{"role":"assistant"}}]}'


class _KeepAliveClient:
    def __init__(self, timeout=None): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    def stream(self, *a, **k): return _KeepAliveStream()


def test_keepalive_traffic_does_not_reset_stall_watchdog(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "k")
    monkeypatch.setattr("runtime.model_client.httpx.AsyncClient", _KeepAliveClient)
    rt, _ = _runtime(_Registry([]), [])
    _cfg(rt, budgets={"stall_s": 0.05})
    out = asyncio.run(rt.run("hi", stream=True))
    assert out["status"] == "stalled"
    assert "no completion content" in out["error"]


def test_spawned_children_run_streamed_for_stall_coverage():
    # The streaming path carries the stall watchdog; the non-streaming one has
    # only the coarse total turn timeout — children must therefore stream.
    captured = {}

    async def child(msg, **kw):
        captured.update(kw)
        return {"status": "ok", "answer": "kid done", "run_id": "sub",
                "budget": {"cost_usd": 0.0, "tokens": {"prompt": 0, "completion": 0}}}

    rt, _ = _spawn_rt([_tc("agent.spawn", json.dumps({"task": "subtask"})),
                       _final("parent done")], child)
    out = asyncio.run(rt.run("delegate this"))
    assert out["status"] == "ok"
    assert captured.get("stream") is True


# ---- batch 6: history cap, batched compaction ----

def test_history_capped_server_side_and_starts_on_user_turn():
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    _cfg(rt, orchestrator={"max_history_messages": 3})
    hist = [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"}, {"role": "assistant", "content": "a3"}]
    out = asyncio.run(rt.run("current question", history=hist))
    assert out["status"] == "ok"
    msgs = seen[0]
    # cap to last 3 ([a2, u3, a3]) then drop the leading assistant turn.
    assert msgs[0]["role"] == "system"
    assert [(m["role"], m["content"]) for m in msgs[1:3]] == [
        ("user", "u3"), ("assistant", "a3")]
    # the datetime note rides between the replayed history and the new turn
    assert msgs[3]["role"] == "system" and "Current date/time" in msgs[3]["content"]
    assert msgs[4] == {"role": "user", "content": "current question"}


def test_history_cap_zero_replays_everything():
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    hist = [{"role": "user", "content": f"u{i}"} for i in range(10)]
    asyncio.run(rt.run("now", history=hist))
    msgs = seen[0]
    assert msgs[0]["role"] == "system"
    assert [m["content"] for m in msgs[1:11]] == [f"u{i}" for i in range(10)]
    assert msgs[11]["role"] == "system"              # datetime note
    assert msgs[12] == {"role": "user", "content": "now"}


def test_compaction_every_n_batches_prefix_breaks():
    class _BigTool(_StubTool):
        async def execute(self, args, ctx):
            return ToolResult(status="ok", result="x" * 500)

    rt, _ = _runtime(
        _Registry([], real={"big.tool": _BigTool("big.tool")}), [])
    rt.config = {**rt.config,
                 "compaction": {"enabled": True, "every": 2,
                                "max_result_chars": 10, "keep_last": 0}}
    turns = [_tc("big.tool", "{}"), _tc("big.tool", "{}"),
             _tc("big.tool", "{}"), _final("done")]
    snaps = []

    async def fake(messages, tools_schema, model=None, think=True, sampling=None):
        # Snapshot: the loop mutates the message dicts in place, so storing
        # the live list would show the FINAL state in every "turn".
        snaps.append([dict(m) for m in messages])
        return {"message": turns.pop(0), "usage": {}}
    rt._model_turn = fake
    out = asyncio.run(rt.run("work"))
    assert out["status"] == "ok"

    def tool_msgs(msgs):
        return [m for m in msgs if m.get("role") == "tool"]

    # Compaction ran on iteration 2: first result stubbed before model turn 2.
    assert "__compacted__" in tool_msgs(snaps[1])[0]["content"]
    # …but iteration 3 was SKIPPED: the second result is still full-size there.
    assert "__compacted__" in tool_msgs(snaps[2])[0]["content"]
    assert "__compacted__" not in tool_msgs(snaps[2])[1]["content"]
    # Iteration 4 runs the pass again: now the second result is stubbed too.
    assert "__compacted__" in tool_msgs(snaps[3])[1]["content"]


# ---- adaptive thinking: trivial runs skip chain-of-thought ----

_SEL_AUTO = {"mode": "auto", "core_namespaces": [],
             "keyword_namespaces": {"code": ["code", "fix", "bug"]}}


def _think_rt(orch_extra):
    """Runtime with an auto-mode selector; records the `think` kwarg of each
    model turn. Script: one final answer, no tool calls."""
    rt, _ = _runtime(_Registry(["fs.read"]), [_final("ok")])
    rt.config["orchestrator"] = {**rt.config["orchestrator"], **orch_extra}
    rt.config["tool_selection"] = dict(_SEL_AUTO)
    rt.selector = ToolSelector(rt.registry, rt.config)
    thinks = []
    orig = rt._model_turn

    async def rec(messages, tools_schema, model=None, think=True, sampling=None):
        thinks.append(think)
        return await orig(messages, tools_schema, model=model, think=think,
                          sampling=sampling)
    rt._model_turn = rec
    return rt, thinks


def test_adaptive_thinking_trivial_run_disables_think():
    rt, thinks = _think_rt({"adaptive_thinking": True})
    out = asyncio.run(rt.run("hello there"))
    assert out["status"] == "ok" and thinks == [False]


def test_adaptive_thinking_keyword_run_keeps_think():
    rt, thinks = _think_rt({"adaptive_thinking": True})
    out = asyncio.run(rt.run("read the code and fix the bug in main.py"))
    assert out["status"] == "ok" and thinks == [True]


def test_adaptive_thinking_long_run_keeps_think():
    rt, thinks = _think_rt({"adaptive_thinking": True})
    msg = "please explain in detail how the prompt cache interacts with " * 3
    out = asyncio.run(rt.run(msg))            # >20 words, no keywords
    assert out["status"] == "ok" and thinks == [True]


def test_adaptive_thinking_disabled_by_default():
    rt, thinks = _think_rt({})                # flag absent -> feature off
    out = asyncio.run(rt.run("hello there"))
    assert out["status"] == "ok" and thinks == [True]


# ---- tool image payloads: shown to the model as image blocks ----

class _ImgTool(_StubTool):
    async def execute(self, args, ctx):
        from runtime.tool_base import ToolResult
        return ToolResult(status="ok", result={"ok": True},
                          images=["data:image/png;base64,AAAA"])


def _img_rt():
    return _runtime(_Registry([], real={"browser.screenshot": _ImgTool("browser.screenshot")}),
                    [_tc("browser.screenshot", "{}"), _final("looked")])


def test_tool_image_appended_when_vision_on():
    rt, seen = _img_rt()
    rt.vision_enabled = True
    out = asyncio.run(rt.run("look at this page"))
    assert out["status"] == "ok"
    msgs = seen[-1]          # the fixture's seen holds the live list (mutated per turn)
    img_msgs = [m for m in msgs if isinstance(m.get("content"), list)
                and any(b.get("type") == "image_url" for b in m["content"])]
    assert len(img_msgs) == 1 and img_msgs[0]["role"] == "user"
    assert img_msgs[0]["content"][0]["text"].startswith("Image output from browser.screenshot")


def test_tool_image_dropped_when_vision_off():
    rt, seen = _img_rt()     # the fixture sets vision_enabled False
    out = asyncio.run(rt.run("look at this page"))
    assert out["status"] == "ok"
    assert not [m for m in seen[-1] if isinstance(m.get("content"), list)
                and any(b.get("type") == "image_url" for b in m["content"])]


def test_compaction_elides_all_but_newest_image():
    from runtime.loop import _compact_messages
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "a"},
                                     {"type": "image_url", "image_url": {"url": "data:1"}}]},
        {"role": "tool", "content": '{"status": "ok"}'},
        {"role": "user", "content": [{"type": "text", "text": "b"},
                                     {"type": "image_url", "image_url": {"url": "data:2"}}]},
    ]
    n = _compact_messages(msgs, {"enabled": True, "max_result_chars": 2000, "keep_last": 3})
    assert n == 1
    first, last = msgs[0]["content"], msgs[2]["content"]
    assert not any(b.get("type") == "image_url" for b in first)
    assert "elided" in first[1]["text"]
    assert any(b.get("type") == "image_url" for b in last)
    assert _compact_messages(msgs, {"enabled": True}) == 0   # idempotent


# ---- run_finish ctx-meter payload ---------------------------------------------

def test_run_finish_reports_first_turn_prompt_tokens():
    """The UI ctx meter calibrates from the FIRST model turn's real prompt fill
    (system+tools+history+message — what /compact shrinks); later turns carry
    the run's own tool noise and must not move the reported number."""
    rt, _ = _runtime(_Registry(["fs.read"]), [])
    _cfg(rt, orchestrator={"context_tokens": 262144})
    prompts = iter([1000, 2600])
    turns = [_tc("fs.read", "{}"), _final("done")]

    async def fake(messages, tools_schema, model=None, think=True, sampling=None):
        return {"message": turns.pop(0),
                "usage": {"prompt_tokens": next(prompts), "completion_tokens": 5}}
    rt._model_turn = fake
    events = []

    async def on_event(ev):
        events.append(ev)

    out = asyncio.run(rt.run("x", on_event=on_event))
    assert out["status"] == "ok"
    fin = [e for e in events if e["type"] == "run_finish"][0]["data"]
    assert fin["prompt_tokens"] == 1000               # FIRST turn, not the last
    assert fin["context_tokens"] == 262144


def test_run_finish_prompt_tokens_window_unset_is_none():
    rt, _ = _runtime(_Registry([]), [_final("hi")])

    async def fake(messages, tools_schema, model=None, think=True, sampling=None):
        return {"message": _final("hi"), "usage": {"prompt_tokens": 42}}
    rt._model_turn = fake
    events = []

    async def on_event(ev):
        events.append(ev)

    asyncio.run(rt.run("x", on_event=on_event))
    fin = [e for e in events if e["type"] == "run_finish"][0]["data"]
    assert fin["prompt_tokens"] == 42
    assert fin["context_tokens"] is None              # no window -> no % shown


# ---- reasoning_content: server-parsed thinking reaches the UI ---------------

def test_streaming_reasoning_content_forwarded_as_reasoning_scope(monkeypatch):
    """llama.cpp splits the template-prefilled <think> block out of content and
    streams it as delta.reasoning_content (LiteLLM passes the field through).
    The loop must forward those deltas as reasoning-scope token events (the
    UI's thinking view) and keep them out of the assembled answer."""
    monkeypatch.setenv("LITELLM_MASTER_KEY", "k")
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"Let me "}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"think."}}]}',
        'data: {"choices":[{"delta":{"content":"The answer is 42."}}]}',
        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":9,"completion_tokens":4}}',
        "data: [DONE]",
    ]
    _SeqClient.scripts = [lines]
    monkeypatch.setattr("runtime.model_client.httpx.AsyncClient", _SeqClient)
    rt, _ = _runtime(_Registry([]), [])
    events = []

    async def on_event(ev):
        events.append(ev)

    out = asyncio.run(rt.run("hi", stream=True, on_event=on_event))
    assert out["status"] == "ok"
    toks = [(e["data"]["scope"], e["data"]["text"])
            for e in events if e["type"] == "token"]
    assert ("reasoning", "Let me ") in toks
    assert ("reasoning", "think.") in toks
    assert ("brain", "The answer is 42.") in toks
    assert out["answer"] == "The answer is 42."       # no think leakage
    assert "think" not in out["answer"]


def test_streaming_inline_think_still_split_when_unparsed(monkeypatch):
    """Fallback for backends that do NOT parse reasoning: a literal
    <think>…</think> inside content is still split into the reasoning scope."""
    monkeypatch.setenv("LITELLM_MASTER_KEY", "k")
    lines = [
        'data: {"choices":[{"delta":{"content":"<think>deep "}}]}',
        'data: {"choices":[{"delta":{"content":"thoughts</think>clean"}}]}',
        'data: {"choices":[{"delta":{"content":" answer"}}]}',
        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":9,"completion_tokens":4}}',
        "data: [DONE]",
    ]
    _SeqClient.scripts = [lines]
    monkeypatch.setattr("runtime.model_client.httpx.AsyncClient", _SeqClient)
    rt, _ = _runtime(_Registry([]), [])
    events = []

    async def on_event(ev):
        events.append(ev)

    out = asyncio.run(rt.run("hi", stream=True, on_event=on_event))
    assert out["status"] == "ok"
    reasoning = "".join(e["data"]["text"] for e in events
                        if e["type"] == "token" and e["data"]["scope"] == "reasoning")
    assert reasoning == "deep thoughts"
    assert out["answer"] == "clean answer"


# ---- privacy gate: tainted cloud calls ask (privacy-flagged), never die ------

class _ExecTool(_StubTool):
    """A stub that really 'runs' (ok result) and can be flagged private."""

    def __init__(self, name, private=False):
        super().__init__(name)
        self.private = private

    async def execute(self, args, ctx):
        return ToolResult(status="ok", result="secret-data", tool_name=self.name)


def _privacy_rt(script):
    """Runtime with a private reader + a cloud tool, and llm.call marked remote."""
    rt, _ = _runtime(_Registry([], real={
        "fs.read": _ExecTool("fs.read", private=True),
        "llm.call": _ExecTool("llm.call"),
    }), script)
    rt.config = {**rt.config, "privacy": {"remote_llm_tools": ["llm.call"]}}
    return rt


class _RecordingConfirm:
    """A confirm_provider that records (name, reason) and returns a fixed verdict."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.asks = []

    async def confirm(self, run_id, name, args, emit, reason=None):
        self.asks.append((name, reason))
        return self.verdict


def test_tainted_cloud_call_asks_privacy_and_proceeds_on_approval():
    rt = _privacy_rt([_tc("fs.read", "{}"),
                      _tc("llm.call", '{"prompt":"summarize this"}'),
                      _final("done")])
    events = []

    async def on_event(ev):
        events.append(ev)

    prov = _RecordingConfirm(True)
    out = asyncio.run(rt.run("x", on_event=on_event, confirm_provider=prov))
    assert out["status"] == "ok" and out["answer"] == "done"
    # asked exactly once (the privacy ask also covers the plain cloud gate),
    # with a reason the UI can warn about
    assert [a[0] for a in prov.asks] == ["llm.call"]
    assert "privacy" in (prov.asks[0][1] or "")
    conf = [e for e in events if e["type"] == "confirmation"]
    assert conf and conf[0]["data"]["via"] == "privacy"
    assert conf[0]["data"]["approved"] is True
    tr = [e for e in events if e["type"] == "tool_result"
          and e["data"]["tool"] == "llm.call"]
    assert tr and tr[0]["data"]["status"] == "ok"


def test_tainted_cloud_call_denied_is_tool_error_run_continues():
    rt = _privacy_rt([_tc("fs.read", "{}"),
                      _tc("llm.call", '{"prompt":"summarize this"}'),
                      _final("fell back to local")])
    prov = _RecordingConfirm(False)
    out = asyncio.run(rt.run("x", confirm_provider=prov))
    assert out["status"] == "ok"                       # run no longer dies
    assert out["answer"] == "fell back to local"
    assert "blocked by privacy" in out["trajectory"]


def test_tainted_cloud_call_without_provider_is_tool_error_not_run_end():
    rt = _privacy_rt([_tc("fs.read", "{}"),
                      _tc("llm.call", '{"prompt":"summarize this"}'),
                      _final("done locally")])
    out = asyncio.run(rt.run("x"))                     # no confirm_provider
    assert out["status"] == "ok"                       # no PrivacyViolation kill
    assert out["answer"] == "done locally"
    assert "blocked by privacy" in out["trajectory"]


def test_share_private_skips_privacy_ask():
    rt = _privacy_rt([_tc("fs.read", "{}"),
                      _tc("llm.call", '{"prompt":"summarize this"}'),
                      _final("done")])
    prov = _RecordingConfirm(True)
    out = asyncio.run(rt.run("x", share_private=True, confirm_provider=prov,
                             auto_confirm=True))
    assert out["status"] == "ok" and out["answer"] == "done"
    assert prov.asks == []                             # blanket opt-in: no gate at all


# ---- loop guard: identical repeats are duplicates only within one mutation
#      generation — re-reading a file after a successful write is fresh data ----

def test_loop_guard_third_identical_read_blocked(tmp_path):
    """Three identical fs.read with NO intervening write: the 3rd is blocked by
    the guard, and the run survives to answer."""
    from tools.fs.ops import FsRead, FsWrite
    (tmp_path / "a.txt").write_text("v1")
    reg = _Registry([], real={"fs.read": FsRead(), "fs.write": FsWrite()})
    script = [
        _tc("fs.read", json.dumps({"path": "a.txt"})),
        _tc("fs.read", json.dumps({"path": "a.txt"})),
        _tc("fs.read", json.dumps({"path": "a.txt"})),     # blocked: gen unchanged
        _final("recovered"),
    ]
    rt, _ = _runtime(reg, script)
    rt.config["confirmation"] = {"enabled": False}          # auto-approve fs.write
    out = asyncio.run(rt.run("loop test", work_root=str(tmp_path)))
    assert out["status"] == "ok" and out["answer"] == "recovered"
    assert "duplicate tool call" in out["trajectory"]


def test_loop_guard_reread_after_write_is_not_duplicate(tmp_path):
    """read, read, WRITE, read, read: all five calls must execute — a re-read
    after a successful write returns new bytes, so the guard must not fire."""
    from tools.fs.ops import FsRead, FsWrite
    reg = _Registry([], real={"fs.read": FsRead(), "fs.write": FsWrite()})
    script = [
        _tc("fs.write", json.dumps({"path": "a.txt", "content": "v1"})),
        _tc("fs.read", json.dumps({"path": "a.txt"})),
        _tc("fs.read", json.dumps({"path": "a.txt"})),
        _tc("fs.write", json.dumps({"path": "a.txt", "content": "v2"})),  # bumps gen
        _tc("fs.read", json.dumps({"path": "a.txt"})),
        _tc("fs.read", json.dumps({"path": "a.txt"})),
        _final("all six ran"),
    ]
    rt, seen = _runtime(reg, script)
    rt.config["confirmation"] = {"enabled": False}
    out = asyncio.run(rt.run("reread test", work_root=str(tmp_path)))
    assert out["status"] == "ok" and out["answer"] == "all six ran"
    assert "duplicate tool call" not in out["trajectory"]
    # and the post-write re-read really saw the new bytes
    tool_msgs = [m for m in seen[-1] if m.get("role") == "tool"]
    assert any("v2" in m["content"] for m in tool_msgs)


# ---- loop guard generations: a successful call by any tool NOT declared
#      read_only invalidates earlier identical calls; pure queries don't ----

class _TouchFile:
    """A mutator stand-in: no read_only attribute, so a successful call must
    bump the loop-guard generation (the safe default for unmarked tools)."""
    private = False
    name = "x.touch"

    def needs_confirmation(self, args, ctx): return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        Path(ctx.work_root, "new.txt").write_text("hi")
        return ToolResult(status="ok", result={"ok": True})


def test_loop_guard_unmarked_mutator_invalidates(tmp_path):
    """list, list, TOUCH, list: the third list must RUN — an unmarked tool's
    success may have changed the directory, so it is never a duplicate."""
    from tools.fs.ops import FsList
    reg = _Registry([], real={"fs.list": FsList(), "x.touch": _TouchFile()})
    script = [
        _tc("fs.list", json.dumps({"path": "."})),
        _tc("fs.list", json.dumps({"path": "."})),
        _tc("x.touch", "{}"),                                  # bumps the generation
        _tc("fs.list", json.dumps({"path": "."})),             # fresh, not a dup
        _final("listed"),
    ]
    rt, _ = _runtime(reg, script)
    out = asyncio.run(rt.run("list around", work_root=str(tmp_path)))
    assert out["status"] == "ok" and out["answer"] == "listed"
    assert "duplicate tool call" not in out["trajectory"]


def test_loop_guard_queries_do_not_invalidate(tmp_path):
    """fs.read alternating with fs.list (both read_only): the 3rd identical
    fs.read is still a duplicate — pure queries change nothing in between."""
    from tools.fs.ops import FsList, FsRead
    (tmp_path / "a.txt").write_text("v1")
    reg = _Registry([], real={"fs.read": FsRead(), "fs.list": FsList()})
    script = [
        _tc("fs.read", json.dumps({"path": "a.txt"})),
        _tc("fs.list", json.dumps({"path": "."})),
        _tc("fs.read", json.dumps({"path": "a.txt"})),
        _tc("fs.list", json.dumps({"path": "."})),
        _tc("fs.read", json.dumps({"path": "a.txt"})),         # blocked
        _final("recovered"),
    ]
    rt, _ = _runtime(reg, script)
    out = asyncio.run(rt.run("read loop", work_root=str(tmp_path)))
    assert out["status"] == "ok" and out["answer"] == "recovered"
    assert "duplicate tool call" in out["trajectory"]


# ---- loop-guard escalation: a model that keeps re-issuing blocked calls gets
#      ONE tools-off wrap-up turn; ignoring that ends the run as "stuck" ------

def _stubborn_runtime(tmp_path, script, max_rejections=6):
    """Runtime + script of identical fs.read turns: turns 1-2 execute, every
    later turn hits the guard. Returns (rt, seen, schemas)."""
    from tools.fs.ops import FsRead
    (tmp_path / "a.txt").write_text("v1")
    reg = _Registry([], real={"fs.read": FsRead()})
    rt, seen = _runtime(reg, script)
    rt.config["budgets"] = {**rt.config["budgets"], "max_iterations": 40}
    rt.config["loop_guard"] = {"max_rejections": max_rejections}
    schemas = []
    orig = rt._model_turn

    async def cap(messages, tools_schema, **kw):
        schemas.append(tools_schema)
        return await orig(messages, tools_schema, **kw)
    rt._model_turn = cap
    return rt, seen, schemas


def test_loop_guard_escalation_forces_wrap_up(tmp_path):
    """After max_rejections refusals the next turn runs with tools DISABLED
    (plus a LOOP GUARD directive); the model's wrap-up answer ends the run."""
    rd = json.dumps({"path": "a.txt"})
    script = [_tc("fs.read", rd)] * 8 + [_final("wrapped up with what I have")]
    rt, seen, schemas = _stubborn_runtime(tmp_path, script, max_rejections=6)
    out = asyncio.run(rt.run("stubborn", work_root=str(tmp_path)))
    assert out["status"] == "ok" and out["answer"] == "wrapped up with what I have"
    assert len(schemas) == 9                       # no 37-iteration ping-pong
    assert schemas[-1] == []                       # wrap-up turn: tools off
    assert all(s != [] for s in schemas[:-1])      # normal turns kept tools
    last_sys = [m for m in seen[-1] if m.get("role") == "system"]
    assert any("LOOP GUARD" in (m.get("content") or "") for m in last_sys)


def test_loop_guard_wrap_up_ignored_ends_stuck(tmp_path):
    """If the model still emits a tool call on the tools-off turn, the run
    ends as 'stuck' instead of re-entering the refusal ping-pong."""
    rd = json.dumps({"path": "a.txt"})
    script = [_tc("fs.read", rd)] * 9                # 9th = the wrap-up turn
    rt, _, schemas = _stubborn_runtime(tmp_path, script, max_rejections=6)
    out = asyncio.run(rt.run("stubborn", work_root=str(tmp_path)))
    assert out["status"] == "stuck"
    assert "loop guard" in out["error"]
    assert schemas[-1] == []                       # the wrap-up turn happened


def test_loop_guard_escalation_disabled_with_zero(tmp_path):
    """max_rejections: 0 keeps the old behaviour — refusals, never a wrap-up
    (the run just hits the iteration budget)."""
    rd = json.dumps({"path": "a.txt"})
    script = [_tc("fs.read", rd)] * 8
    rt, _, schemas = _stubborn_runtime(tmp_path, script, max_rejections=0)
    rt.config["budgets"]["max_iterations"] = 8
    out = asyncio.run(rt.run("stubborn", work_root=str(tmp_path)))
    assert out["status"] == "budget_exceeded"
    assert all(s != [] for s in schemas)           # tools never disabled


# ---- tools.load: mid-run toolset expansion ------------------------------------

def _expand_runtime(script, max_expansions=2):
    """Auto-mode runtime with a tiny core set (tools.load + web.search), real
    ToolsLoad, schema capture per turn. Returns (rt, seen, schemas)."""
    from tools.tools.load import ToolsLoad
    reg = _Registry(["fs.read", "fs.write", "git.status", "web.search"],
                    real={"tools.load": ToolsLoad()})
    rt, seen = _runtime(reg, script)
    rt.config["tool_selection"] = {
        "mode": "auto", "core_namespaces": ["tools.load", "web.search"],
        "keyword_namespaces": {}, "max_expansions": max_expansions,
    }
    rt.selector = ToolSelector(reg, rt.config)
    schemas = []
    orig = rt._model_turn

    async def cap(messages, tools_schema, **kw):
        schemas.append([s["function"]["name"] for s in tools_schema])
        return await orig(messages, tools_schema, **kw)
    rt._model_turn = cap
    return rt, seen, schemas


def test_tools_load_expands_toolset_mid_run():
    script = [_tc("tools.load", json.dumps({"namespaces": ["fs"]})),
              _final("saved")]
    rt, seen, schemas = _expand_runtime(script)
    out = asyncio.run(rt.run("please save this"))
    assert out["status"] == "ok"
    assert "fs.write" not in schemas[0] and "tools.load" in schemas[0]
    assert "fs.read" in schemas[1] and "fs.write" in schemas[1]
    assert "git.status" not in schemas[1]           # only what was asked for
    tool_msgs = [m for m in seen[1] if m.get("role") == "tool"]
    assert any("fs.write" in (m.get("content") or "") for m in tool_msgs)


def test_tools_load_expansion_cap():
    script = [_tc("tools.load", json.dumps({"namespaces": ["fs"]})),
              _tc("tools.load", json.dumps({"namespaces": ["git"]})),
              _final("done")]
    rt, seen, schemas = _expand_runtime(script, max_expansions=1)
    out = asyncio.run(rt.run("work on files and git"))
    assert out["status"] == "ok"
    assert "fs.write" in schemas[1]
    assert "git.status" not in schemas[-1]          # second load refused
    tool_msgs = [m for m in seen[2] if m.get("role") == "tool"]
    assert any("limit reached" in (m.get("content") or "") for m in tool_msgs)


def test_tools_load_refused_when_caller_fixed():
    from tools.tools.load import ToolsLoad
    script = [_tc("tools.load", json.dumps({"namespaces": ["fs"]})),
              _final("done")]
    reg = _Registry(["tools.load", "fs.write"], real={"tools.load": ToolsLoad()})
    rt, seen = _runtime(reg, script)
    out = asyncio.run(rt.run("x", tools=["tools.load"]))
    assert out["status"] == "ok"
    tool_msgs = [m for m in seen[1] if m.get("role") == "tool"]
    assert any("fixed by the caller" in (m.get("content") or "") for m in tool_msgs)


def test_tools_load_noop_when_all_tools_exposed():
    from tools.tools.load import ToolsLoad
    script = [_tc("tools.load", json.dumps({"namespaces": ["fs"]})),
              _final("done")]
    reg = _Registry(["fs.write"], real={"tools.load": ToolsLoad()})
    rt, seen = _runtime(reg, script)                # default CFG: mode "all"
    out = asyncio.run(rt.run("x"))
    assert out["status"] == "ok"
    tool_msgs = [m for m in seen[1] if m.get("role") == "tool"]
    assert any("already available" in (m.get("content") or "") for m in tool_msgs)


def test_tools_load_unknown_namespace_errors_and_keeps_cap():
    script = [_tc("tools.load", json.dumps({"namespaces": ["nope"]})),
              _tc("tools.load", json.dumps({"namespaces": ["fs"]})),
              _final("done")]
    rt, seen, schemas = _expand_runtime(script, max_expansions=1)
    out = asyncio.run(rt.run("do something"))
    assert out["status"] == "ok"
    tool_msgs = [m for m in seen[1] if m.get("role") == "tool"]
    assert any("unknown tool or category" in (m.get("content") or "")
               for m in tool_msgs)
    assert schemas[0] == schemas[1]                 # no rebuild on failure
    assert "fs.write" in schemas[2]                 # failure didn't burn the cap


def test_tools_load_respects_disabled():
    script = [_tc("tools.load", json.dumps({"namespaces": ["fs"]})),
              _final("done")]
    rt, seen, schemas = _expand_runtime(script)
    out = asyncio.run(rt.run("x", disabled_tools={"fs.write"}))
    assert out["status"] == "ok"
    assert "fs.read" in schemas[1] and "fs.write" not in schemas[1]


def test_tools_load_without_runtime_seam():
    from tools.tools.load import ToolsLoad

    class _Ctx:
        expand_tools = None

    r = asyncio.run(ToolsLoad().execute({"namespaces": ["fs"]}, _Ctx()))
    assert r.status == "error" and "not available" in r.error
    r = asyncio.run(ToolsLoad().execute({"namespaces": []}, _Ctx()))
    assert r.status == "error" and "required" in r.error


# ---- run-level sampling vs non-brain models (eval benchmark B1) -------------

def _sampling_capture(rt):
    samplings = []
    base = rt._model_turn

    async def cap(messages, tools_schema, model=None, think=True, sampling=None):
        samplings.append(sampling)
        return await base(messages, tools_schema, model=model, think=think,
                          sampling=sampling)
    rt._model_turn = cap
    return samplings


def test_run_sampling_brain_default_and_cross_model_guard():
    rt, _ = _runtime(_Registry([]), [_final(), _final(), _final()])
    samplings = _sampling_capture(rt)
    # brain run: config sampling (empty here) + the 0.7 fallback
    asyncio.run(rt.run("one"))
    assert samplings[0] == {"temperature": 0.7}
    # cross-model override WITHOUT force: sampling stays None (server preset)
    asyncio.run(rt.run("two", model="local-specialist",
                       run_overrides={"sampling": {"temperature": 0}}))
    assert samplings[1] is None
    # cross-model override WITH force (eval benchmark variants): pinned sampling
    asyncio.run(rt.run("three", model="local-specialist",
                       run_overrides={"sampling": {"temperature": 0, "seed": 42},
                                      "sampling_force": True}))
    assert samplings[2] == {"temperature": 0, "seed": 42}


# ---- near-duplicate loop guard: reworded repeats of a query-like tool ----

class _FakeSearch:
    """read_only web.search stand-in (read_only so successful calls do NOT
    bump the loop-guard mutation generation)."""
    private = False
    read_only = True
    name = "web.search"

    def needs_confirmation(self, args, ctx): return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        return ToolResult(status="ok", result={"hits": [args.get("query")]})


def _search_rt(script, **lg):
    reg = _Registry([], real={"web.search": _FakeSearch()})
    rt, seen = _runtime(reg, script)
    rt.config["loop_guard"] = {"max_rejections": 6,
                               "near_dup_threshold": 0.75,
                               "near_dup_tools": ["web.search"], **lg}
    return rt, seen


def test_near_dup_reworded_search_blocked():
    """The coroner failure mode: the SAME search reworded. Two refinements
    pass, the third near-identical variant is blocked with a synthesize-now
    error — and the run recovers to answer."""
    script = [
        _tc("web.search", json.dumps({"query": "Geneva City Pass price 2026 CHF"})),
        _tc("web.search", json.dumps({"query": "Geneva City Pass 24h price CHF 2026"})),
        _tc("web.search", json.dumps({"query": "2026 Geneva City Pass CHF price 24h"})),
        _final("synthesized"),
    ]
    rt, _ = _search_rt(script)
    out = asyncio.run(rt.run("research test"))
    assert out["status"] == "ok" and out["answer"] == "synthesized"
    assert "near-duplicate tool call" in out["trajectory"]


def test_near_dup_distinct_queries_pass():
    """Genuinely different queries score far below the threshold and all run
    (deep research must not be strangled)."""
    script = [
        _tc("web.search", json.dumps({"query": "Geneva City Pass price 2026 CHF"})),
        _tc("web.search", json.dumps({"query": "Engadin stargazing events august"})),
        _tc("web.search", json.dumps({"query": "Bodensee astronomy guided tours"})),
        _tc("web.search", json.dumps({"query": "Swiss museum night opening hours"})),
        _final("all four ran"),
    ]
    rt, _ = _search_rt(script)
    out = asyncio.run(rt.run("research test"))
    assert out["status"] == "ok" and out["answer"] == "all four ran"
    assert "near-duplicate tool call" not in out["trajectory"]


def test_near_dup_disabled_with_zero_threshold():
    script = [
        _tc("web.search", json.dumps({"query": "Geneva City Pass price 2026 CHF"})),
        _tc("web.search", json.dumps({"query": "Geneva City Pass 24h price CHF 2026"})),
        _tc("web.search", json.dumps({"query": "2026 Geneva City Pass CHF price 24h"})),
        _final("all three ran"),
    ]
    rt, _ = _search_rt(script, near_dup_threshold=0)
    out = asyncio.run(rt.run("research test"))
    assert out["status"] == "ok" and out["answer"] == "all three ran"
    assert "near-duplicate tool call" not in out["trajectory"]


# ---- failure-loop escalation: N consecutive same-signature execution failures
#      earn a strategy-change hint; success or a different error resets ----

class _FakeRunner:
    """code.run stand-in: reports failures the way code.run really does —
    ToolResult status ok, the failure lives in the payload (ok/exit_code)."""
    private = False
    name = "code.run"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def needs_confirmation(self, args, ctx): return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        rc, err = self.outcomes.pop(0) if self.outcomes else (0, "")
        return ToolResult(status="ok", tool_name=self.name,
                          result={"exit_code": rc, "ok": rc == 0,
                                  "stdout": "", "stderr": err})


_SEGV = (-11, "Segmentation fault (core dumped)")


def _crash_rt(script, outcomes, extra_real=None, **lg):
    real = {"code.run": _FakeRunner(outcomes)}
    real.update(extra_real or {})
    reg = _Registry([], real=real)
    rt, seen = _runtime(reg, script)
    rt.config["loop_guard"] = {"max_rejections": 6, **lg}
    out = asyncio.run(rt.run("crash loop", work_root=tempfile.mkdtemp()))
    tool_msgs = [m for msgs in seen for m in msgs if m.get("role") == "tool"]
    return out, tool_msgs


def test_failure_nudge_at_threshold_with_delegate_hint():
    """The live bench failure mode: the same solver rebuilt and segfaulting
    over and over. From the 3rd consecutive same-signature failure the tool
    result carries a strategy-change hint — pointing at code.delegate when
    the specialist is actually reachable in this run."""
    script = [_tc("code.run", "{}"), _tc("code.run", "{}"), _tc("code.run", "{}"),
              _final("gave up")]
    out, msgs = _crash_rt(script, [_SEGV, _SEGV, _SEGV],
                          extra_real={"code.delegate": _StubTool("code.delegate")})
    assert out["status"] == "ok"
    hinted = [m["content"] for m in msgs if "consecutive executions failed" in m["content"]]
    assert hinted, "no escalation hint after 3 identical crashes"
    assert "code.delegate" in hinted[-1]


def test_failure_nudge_resets_on_success():
    script = [_tc("code.run", "{}"), _tc("code.run", "{}"), _tc("code.run", "{}"),
              _tc("code.run", "{}"), _tc("code.run", "{}"), _final("done")]
    out, msgs = _crash_rt(script, [_SEGV, _SEGV, (0, ""), _SEGV, _SEGV])
    assert out["status"] == "ok"
    assert not any("consecutive executions failed" in m["content"] for m in msgs)


def test_failure_nudge_resets_on_new_signature():
    """Two crashes of one kind, then a different error: the count restarts —
    a model that changed approach must not be nagged."""
    script = [_tc("code.run", "{}"), _tc("code.run", "{}"), _tc("code.run", "{}"),
              _tc("code.run", "{}"), _final("done")]
    out, msgs = _crash_rt(script, [_SEGV, _SEGV, (1, "NameError: x"),
                                   (1, "NameError: y")])
    assert out["status"] == "ok"
    assert not any("consecutive executions failed" in m["content"] for m in msgs)


def test_failure_nudge_disabled_with_zero():
    script = [_tc("code.run", "{}"), _tc("code.run", "{}"), _tc("code.run", "{}"),
              _tc("code.run", "{}"), _final("done")]
    out, msgs = _crash_rt(script, [_SEGV] * 4, failure_nudge_after=0)
    assert out["status"] == "ok"
    assert not any("consecutive executions failed" in m["content"] for m in msgs)


# ---- delegate gate: inline implementation while a coder specialist sits ----
# ---- unused earns a directive; enforce mode closes inline edits        ----

class _WriteTool:
    """fs.write/fs.edit stand-in: always succeeds."""
    private = False

    def __init__(self, name):
        self.name = name

    def needs_confirmation(self, args, ctx): return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        return ToolResult(status="ok", tool_name=self.name,
                          result={"path": "x.py", "action": "written"})


class _DelegateProbe(_WriteTool):
    """code.delegate stand-in: records that it was called, returns ok."""

    def __init__(self):
        super().__init__("code.delegate")
        self.calls = 0

    async def execute(self, args, ctx):
        self.calls += 1
        return ToolResult(status="ok", tool_name=self.name,
                          result={"report": "child done"})


def _gate_rt(script, probe=None, specialist=True, **lg):
    real = {"fs.write": _WriteTool("fs.write"),
            "fs.edit": _WriteTool("fs.edit")}
    if probe is not None:
        real["code.delegate"] = probe
    rt, seen = _runtime(_Registry([], real=real), script)
    rt.config["loop_guard"] = {"max_rejections": 6, **lg}
    if specialist:
        # A configured coder alias makes delegation "route somewhere
        # stronger" without probing live slots (see delegate_ok).
        rt.config["tools"] = {"code": {"delegate": {"model": "coder-alias"}}}
    out = asyncio.run(rt.run("build the thing", work_root=tempfile.mkdtemp()))
    # seen snapshots alias the same growing message list — dedupe by object
    # identity so each tool message is asserted on exactly once.
    tool_msgs, ids = [], set()
    for msgs in seen:
        for m in msgs:
            if m.get("role") == "tool" and id(m) not in ids:
                ids.add(id(m))
                tool_msgs.append(m)
    return out, tool_msgs


def test_delegate_gate_nudges_at_threshold():
    """The live eval failure mode: the brain implements inline (17 edits, 0
    delegations) though its prompt says to delegate. From the 3rd inline
    write the tool result carries a delegate directive."""
    script = [_tc("fs.write", "{}"), _tc("fs.write", "{}"), _tc("fs.write", "{}"),
              _final("done")]
    out, msgs = _gate_rt(script, probe=_DelegateProbe())
    assert out["status"] == "ok"
    hinted = [m["content"] for m in msgs if "non-trivial coding" in m["content"]]
    assert len(hinted) == 1 and "code.delegate" in hinted[0]


def test_delegate_gate_silent_without_delegate_tool():
    """Runs whose toolset has no code.delegate (brain-variant evals, narrow
    chats) are never touched by the gate."""
    script = [_tc("fs.write", "{}"), _tc("fs.write", "{}"), _tc("fs.write", "{}"),
              _final("done")]
    out, msgs = _gate_rt(script, probe=None)
    assert out["status"] == "ok"
    assert not any("non-trivial coding" in m["content"] for m in msgs)


def test_delegate_gate_disarmed_by_delegate_call():
    """Once the brain delegates, the gate goes quiet — further inline edits
    (verify-fix loops on the child's report) are legitimate."""
    script = [_tc("fs.write", "{}"), _tc("code.delegate", "{}"),
              _tc("fs.write", "{}"), _tc("fs.write", "{}"), _final("done")]
    probe = _DelegateProbe()
    out, msgs = _gate_rt(script, probe=probe, delegate_nudge_after=2)
    assert out["status"] == "ok" and probe.calls == 1
    assert not any("non-trivial coding" in m["content"] for m in msgs)


def test_delegate_gate_enforce_rejects_first_write_then_disarms():
    """Hard mode, threshold 1: the FIRST inline write is rejected —
    delegate first, literally — and one code.delegate call reopens inline
    edits (verify-fix loops on the child's report stay legitimate)."""
    script = [_tc("fs.write", "{}"),          # rejected pre-exec (after=1)
              _tc("code.delegate", "{}"),     # disarms the gate
              _tc("fs.write", "{}"),          # executes again
              _final("done")]
    probe = _DelegateProbe()
    out, msgs = _gate_rt(script, probe=probe,
                         delegate_nudge_after=1, delegate_enforce=True)
    assert out["status"] == "ok" and probe.calls == 1
    contents = [m["content"] for m in msgs]
    rejected = [c for c in contents if "inline implementation is closed" in c]
    assert len(rejected) == 1
    oks = [m for m in msgs if m.get("name") == "fs.write"
           and '"action": "written"' in m["content"]]
    assert len(oks) == 1
    # enforce mode rejects at the threshold — no soft directive alongside
    assert not any("non-trivial coding" in c for c in contents)


def test_delegate_gate_enforce_allows_writes_below_threshold():
    """Threshold 3: two inline writes pass, the third is rejected."""
    script = [_tc("fs.write", "{}"), _tc("fs.write", "{}"),
              _tc("fs.edit", "{}"),            # rejected
              _tc("code.delegate", "{}"),
              _tc("fs.write", "{}"),           # ok again
              _final("done")]
    probe = _DelegateProbe()
    out, msgs = _gate_rt(script, probe=probe,
                         delegate_nudge_after=3, delegate_enforce=True)
    assert out["status"] == "ok" and probe.calls == 1
    rejected = [m["content"] for m in msgs
                if "inline implementation is closed" in m["content"]]
    assert len(rejected) == 1
    oks = [m for m in msgs if m.get("name") in ("fs.write", "fs.edit")
           and '"action": "written"' in m["content"]]
    assert len(oks) == 3


def test_delegate_gate_silent_without_specialist_route(monkeypatch):
    """code.delegate registered but routing nowhere (no configured coder,
    no live coding-strength specialist — the single-model install): the
    gate stays silent rather than forcing same-model child spawns."""
    import tools.model.catalog as catalog

    async def no_route(config, wanted):
        return None
    monkeypatch.setattr(catalog, "route_strength", no_route)
    script = [_tc("fs.write", "{}"), _tc("fs.write", "{}"), _tc("fs.write", "{}"),
              _final("done")]
    out, msgs = _gate_rt(script, probe=_DelegateProbe(), specialist=False,
                         delegate_nudge_after=1, delegate_enforce=True)
    assert out["status"] == "ok"
    assert not any("inline implementation is closed" in m["content"]
                   for m in msgs)
    assert not any("non-trivial coding" in m["content"] for m in msgs)


def test_exec_failure_signature_stable_across_builds():
    """Digits and addresses vary between rebuilds; the crash signature must
    not — that stability is what makes 'same approach' detectable."""
    from runtime.loop import _exec_failure
    r1 = ToolResult(status="ok", result={"exit_code": -11, "ok": False,
                                         "stderr": "segfault at 0xdeadbeef addr 42"})
    r2 = ToolResult(status="ok", result={"exit_code": -11, "ok": False,
                                         "stderr": "segfault at 0x1234 addr 98765"})
    f1, s1 = _exec_failure("code.run", r1)
    f2, s2 = _exec_failure("code.run", r2)
    assert f1 and f2 and s1 == s2
    ok = ToolResult(status="ok", result={"exit_code": 0, "ok": True, "stderr": ""})
    assert _exec_failure("code.run", ok) == (False, None)


def test_arg_tokens_values_only_and_jaccard():
    """Keys are excluded (constant per tool); values normalize case/length."""
    a = AgentRuntime._arg_tokens({"query": "Geneva City Pass price 2026 CHF"})
    b = AgentRuntime._arg_tokens({"query": "Geneva City Pass 24h price CHF 2026"})
    assert "query" not in a                       # key, not a value
    assert "chf" in a and "24h" not in a
    assert AgentRuntime._jaccard(a, b) >= 0.75    # the coroner's pair IS a dup
    c = AgentRuntime._arg_tokens({"query": "Engadin stargazing events"})
    assert AgentRuntime._jaccard(a, c) < 0.5
    assert AgentRuntime._jaccard(frozenset(), a) == 0.0


# ---- selector: the shipped config routes "too big" messages to context.* ----

def test_select_auto_context_keyword_family_shipped():
    # context.stage is the habit tool of the long-document pattern — it must
    # be reachable via the shipped keyword route, not only via tools.load.
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent /
                          "config" / "runtime.yaml").read_text())
    s = _sel(["context.pin", "context.stage", "web.search"], cfg)
    got = s.select("this log is too big to paste — summarise it")
    assert got is not None
    assert "context.stage" in got
