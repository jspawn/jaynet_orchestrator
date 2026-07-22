"""Slash commands: /help rendering, arg parsing, direct tool execution,
confirmation gating — all off-registry, no model involved."""
import asyncio

from runtime.slash import (help_overview, help_tool, help_tools, parse_tool_args,
                           run_slash)
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
