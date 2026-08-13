"""Slash commands: /help rendering, arg parsing, direct tool execution,
confirmation gating — all off-registry, no model involved."""
import asyncio

from runtime.slash import help_overview, help_tool, help_tools, parse_tool_args, run_slash
from runtime.tool_base import Tool, ToolContext, ToolResult


class _Echo(Tool):
    name = "test.echo"
    description = "Echo the given text back."
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to echo."},
            "upper": {"type": "boolean", "description": "Shout it."},
            "n": {"type": "integer", "description": "Repeat count."},
        },
        "required": ["text"],
    }

    async def execute(self, args, ctx):
        out = args["text"].upper() if args.get("upper") else args["text"]
        return ToolResult(status="ok", result={"echo": out * int(args.get("n") or 1)})


class _Gated(Tool):
    name = "test.gated"
    description = "Needs approval."
    requires_confirmation = True
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args, ctx):
        return ToolResult(status="ok", result={"ran": True})


class _Reg:
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}

    def all(self):
        return list(self._tools.values())

    def get(self, name):
        return self._tools.get(name)


REG = _Reg([_Echo(), _Gated()])
CTX = ToolContext(request_id="t", config={}, budget=None)


def _run(cmd, confirm=None):
    return asyncio.run(run_slash(cmd, REG, CTX, confirm))


def test_help_overview_lists_namespaces():
    out = help_overview(REG)
    assert "/help tools" in out and "test (2)" in out


def test_help_tools_groups():
    out = help_tools(REG)
    assert "`test.echo`" in out and "`test.gated`" in out


def test_help_tool_card():
    out = help_tool(_Echo())
    assert "**`test.echo`**" in out and "private" in out
    assert "`text`: string *(required)*" in out
    assert "Echo the given text back." in out


def test_help_unknown_tool():
    assert "no tool named" in _run("/help test.nope")


def test_help_meta_topics():
    """`/help <meta-command>` — the composer suggests these after `/help `, so
    each must have its own card (the registry only knows tools)."""
    assert "model impersonator" in _run("/help imp")
    assert "impersonation" in _run("/help impstop")
    assert "continuity brief" in _run("/help compact")
    assert "skill-authoring" in _run("/help wgs")
    assert "/help tools" in _run("/help help")


def test_parse_args_variants():
    assert parse_tool_args(_Echo(), "") == {}
    assert parse_tool_args(_Echo(), '{"text": "hi", "n": 2}') == {"text": "hi", "n": 2}
    assert parse_tool_args(_Echo(), 'text=hi n=2 upper=true') == {"text": "hi", "n": 2, "upper": True}
    assert parse_tool_args(_Echo(), "hello") == {"text": "hello"}   # bare -> sole required
    try:
        parse_tool_args(_Echo(), "two words")
        assert False, "should have raised"
    except ValueError as e:
        assert "key=value" in str(e)


def test_slash_executes_tool_with_coercion():
    out = _run("/test.echo text=hi n=2 upper=true")
    assert '"echo": "HIHI"' in out


def test_slash_bare_value():
    assert '"echo": "hello"' in _run("/test.echo hello")


def test_unknown_command():
    assert "unknown command" in _run("/nope")


def test_gated_tool_confirmation_paths():
    ran = {"asked": False}

    async def deny(name, args):
        ran["asked"] = True
        return False

    out = _run("/test.gated", confirm=deny)
    assert ran["asked"] and "declined" in out

    async def allow(name, args):
        return True

    out = _run("/test.gated", confirm=allow)
    assert '"ran": true' in out


def test_gated_tool_no_hook_is_denied():
    assert "declined" in _run("/test.gated")          # confirm=None -> refuse


# ---- slash-spawned sub-agents (runtime.loop.slash_spawn) ----
# A slashed tool runs in a bare ToolContext; spawn-dependent tools
# (code.delegate, agent.spawn, …) used to die with "sub-agents are not
# available in this runtime". slash_spawn wires a real depth-1 child run.

from runtime.loop import slash_spawn


class _FakeRuntime:
    def __init__(self):
        self.config = {"agent": {"default_budget": {"max_cost_usd": 0.5},
                                 "default_sub_iterations": 5}}
        self.model = "local-orchestrator"
        self.calls = []

    async def run(self, task, **kw):
        self.calls.append((task, kw))
        return {"status": "ok", "answer": "child done", "run_id": "child-1",
                "budget": {"iterations": 1}}


class _Spawner(Tool):
    name = "test.spawn"
    description = "Needs ctx.spawn."
    private = True
    parameters = {"type": "object",
                  "properties": {"task": {"type": "string"}},
                  "required": ["task"]}

    async def execute(self, args, ctx):
        if ctx.spawn is None:
            return ToolResult(status="error",
                              error="sub-agents are not available in this runtime")
        child = await ctx.spawn(args["task"])
        return ToolResult(status="ok", result=child)


def test_slash_spawn_runs_depth1_child_with_config_caps():
    rt = _FakeRuntime()
    spawn = slash_spawn(rt)
    child = asyncio.run(spawn("do X", tools=["fs.read"], model="m",
                              budget={"max_cost_usd": 0.1}))
    task, kw = rt.calls[0]
    assert task == "do X"
    assert kw["depth"] == 1 and kw["tools"] == ["fs.read"] and kw["model"] == "m"
    assert kw["budget_overrides"]["max_cost_usd"] == 0.1     # call wins over config
    assert kw["budget_overrides"]["max_iterations"] == 5     # config default fills in
    assert kw["stream"] is True
    assert child["status"] == "ok"


def test_slash_spawn_no_budget_uses_config_defaults():
    rt = _FakeRuntime()
    asyncio.run(slash_spawn(rt)("do Y"))
    _, kw = rt.calls[0]
    assert kw["budget_overrides"] == {"max_cost_usd": 0.5, "max_iterations": 5}


def test_slash_spawn_forwards_lifecycle_and_progress():
    events = []

    async def emit(t, d):
        events.append((t, d))

    rt = _FakeRuntime()

    async def run(task, **kw):
        await kw["on_event"]({"type": "tool_result",
                              "data": {"tool": "fs.read", "status": "ok"}})
        await kw["on_event"]({"type": "model_turn",
                              "data": {"content": "working on it"}})
        return {"status": "ok", "run_id": "c", "budget": {}}

    rt.run = run
    asyncio.run(slash_spawn(rt, run_id="r1", emit=emit)("t"))
    types = [t for t, _ in events]
    assert types[0] == "subagent_start" and types[-1] == "subagent_finish"
    labels = [d["label"] for t, d in events if t == "progress"]
    assert any("fs.read ✓" in lb for lb in labels)
    assert any("working on it" in lb for lb in labels)


def test_slash_spawn_routes_confirm_to_parent_run():
    confirms = []

    class _Provider:
        async def confirm(self, run_id, name, args, emit, reason=None):
            confirms.append((run_id, name))
            return True

    rt = _FakeRuntime()

    async def run(task, **kw):
        assert await kw["confirm_provider"].confirm("child-run", "fs.write", {}, None)
        return {"status": "ok", "run_id": "c", "budget": {}}

    rt.run = run
    asyncio.run(slash_spawn(rt, run_id="parent-run",
                            confirm_provider=_Provider())("t"))
    assert confirms == [("parent-run", "fs.write")]


def test_slash_spawned_tool_no_longer_errors():
    """The reported bug: /code.delegate via slash -> "sub-agents are not
    available". With slash_spawn wired into the ctx, the child runs."""
    rt = _FakeRuntime()
    ctx = ToolContext(request_id="t", config={}, budget=None)
    ctx.spawn = slash_spawn(rt)
    out = asyncio.run(run_slash("/test.spawn task=hello", _Reg([_Spawner()]), ctx))
    assert "sub-agents are not available" not in out
    assert '"answer": "child done"' in out
