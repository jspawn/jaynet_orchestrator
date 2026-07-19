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
    monkeypatch.setattr("runtime.loop.httpx.AsyncClient", _SilentClient)
    rt, _ = _runtime(_Registry([]), [])
    _cfg(rt, budgets={"stall_s": 0.05})
    out = asyncio.run(rt.run("hi", stream=True))
    assert out["status"] == "stalled"
    assert "no streamed output" in out["error"]        # silence, not a timeout
    assert "[Run terminated: model stalled]" in out["answer"]
    assert "Partial result" in out["answer"]


def test_streaming_total_turn_timeout_ends_run_stalled(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "k")
    monkeypatch.setattr("runtime.loop.httpx.AsyncClient", _DripClient)
    rt, _ = _runtime(_Registry([]), [])
    _cfg(rt, orchestrator={"turn_timeout_s": 0.05}, budgets={"stall_s": 60})
    out = asyncio.run(rt.run("hi", stream=True))
    assert out["status"] == "stalled"
    assert "total turn timeout" in out["error"]        # timeout, not silence


def test_turn_timeout_ends_run_stalled_not_internal_error(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "k")
    monkeypatch.setattr("runtime.loop.httpx.AsyncClient", _TimeoutClient)
    rt, _ = _runtime(_Registry([]), [])
    del rt._model_turn                                 # use the REAL non-streaming turn
    _cfg(rt, orchestrator={"turn_timeout_s": 7})
    out = asyncio.run(rt.run("hi"))                    # stream=False path
    assert out["status"] == "stalled"
    assert "total turn timeout" in out["error"]
    assert "Internal error" not in out["answer"]


def test_stall_watchdog_never_fires_during_tool_execution(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "k")
    monkeypatch.setattr("runtime.loop.httpx.AsyncClient", _SeqClient)
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
    monkeypatch.setattr("runtime.loop.httpx.AsyncClient", _RecClient)
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

def test_datetime_appended_last_keeps_static_prefix_cacheable():
    # The datetime is the only per-run-varying part of the system prefix; it
    # must come AFTER every stable section (catalog, extra, grill, workspace),
    # or each new run re-prefills kilotokens of cached prefix.
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    rt.skill_catalog = "SKILLCATALOG"
    asyncio.run(rt.run("hi", grill=True, extra_system="EXTRASYS",
                       work_root="/tmp/wk"))
    sysmsg = seen[0][0]["content"]
    dt = sysmsg.find("Current date/time")
    assert dt != -1
    for marker in ("SKILLCATALOG", "EXTRASYS", "Grill me", "Your workspace"):
        pos = sysmsg.find(marker)
        assert pos != -1 and pos < dt, marker


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
    monkeypatch.setattr("runtime.loop.httpx.AsyncClient", _KeepAliveClient)
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
    assert msgs[3] == {"role": "user", "content": "current question"}


def test_history_cap_zero_replays_everything():
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    hist = [{"role": "user", "content": f"u{i}"} for i in range(10)]
    asyncio.run(rt.run("now", history=hist))
    msgs = seen[0]
    assert msgs[0]["role"] == "system"
    assert [m["content"] for m in msgs[1:11]] == [f"u{i}" for i in range(10)]
    assert msgs[11] == {"role": "user", "content": "now"}


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
