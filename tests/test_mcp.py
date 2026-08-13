"""Tests for the MCP bridge tools (mcp.list / mcp.call).

No real MCP server and no `mcp` package needed: the client layer is
monkeypatched at the module boundary, exactly where the SDK would be called.
"""
from __future__ import annotations

import pytest
from conftest import run

from runtime.tool_base import ToolContext
from tools.mcp import client
from tools.mcp.call import McpCall
from tools.mcp.list import McpList


def _ctx(servers=None, **kw):
    cfg = {"tools": {"mcp": {"timeout_s": 5, "servers": servers or {}}}}
    return ToolContext(request_id="t", config=cfg, budget=None, **kw)


@pytest.fixture(autouse=True)
def _clear_cache():
    client.reset_cache()
    yield
    client.reset_cache()


# ---- mcp.list -------------------------------------------------------------

def test_list_no_servers():
    res = run(McpList().execute({}, _ctx()))
    assert res.status == "ok"
    assert res.result["servers"] == []
    assert "no MCP servers" in res.result["note"]


def test_list_servers_metadata_only():
    servers = {
        "fs": {"command": "npx", "args": ["-y", "srv"]},
        "api": {"url": "http://x/mcp", "confirm": False},
    }
    res = run(McpList().execute({}, _ctx(servers)))
    assert res.status == "ok"
    by_name = {s["name"]: s for s in res.result["servers"]}
    assert by_name["fs"]["transport"] == "stdio"
    assert by_name["fs"]["confirm"] is True          # default: gated
    assert by_name["api"]["transport"] == "http"
    assert by_name["api"]["confirm"] is False


def test_list_server_tools(monkeypatch):
    async def fake_list(config, name, timeout):
        return [{"name": "read_file", "description": "read a file"}]
    monkeypatch.setattr(client, "list_tools", fake_list)
    res = run(McpList().execute({"server": "fs"},
                                _ctx({"fs": {"command": "npx"}})))
    assert res.status == "ok"
    assert res.result["tools"][0]["name"] == "read_file"


def test_list_unknown_server():
    res = run(McpList().execute({"server": "nope"},
                                _ctx({"fs": {"command": "npx"}})))
    assert res.status == "error"
    assert "unknown MCP server 'nope'" in res.error
    assert "fs" in res.error                          # names what's configured


# ---- mcp.call -------------------------------------------------------------

def test_call_success(monkeypatch):
    seen = {}

    async def fake_call(config, name, tool, arguments, timeout):
        seen.update(name=name, tool=tool, arguments=arguments, timeout=timeout)
        return "file contents"
    monkeypatch.setattr(client, "call_tool", fake_call)
    res = run(McpCall().execute(
        {"server": "fs", "tool": "read_file", "arguments": {"path": "/a"}},
        _ctx({"fs": {"command": "npx", "timeout_s": 9}})))
    assert res.status == "ok"
    assert res.result == "file contents"
    assert seen == {"name": "fs", "tool": "read_file",
                    "arguments": {"path": "/a"}, "timeout": 9.0}


def test_call_private_flag():
    # MCP results must never flow to a cloud LLM without consent.
    assert McpCall().private is True


def test_call_missing_args():
    res = run(McpCall().execute({"server": "fs"}, _ctx({"fs": {"command": "x"}})))
    assert res.status == "error"
    assert "required" in res.error


def test_call_arguments_must_be_object():
    res = run(McpCall().execute(
        {"server": "fs", "tool": "t", "arguments": ["not", "a", "dict"]},
        _ctx({"fs": {"command": "x"}})))
    assert res.status == "error"
    assert "JSON object" in res.error


def test_call_unknown_server_gated_and_errors():
    tool = McpCall()
    ctx = _ctx({"fs": {"command": "x"}})
    assert tool.needs_confirmation({"server": "nope", "tool": "t"}, ctx) is True
    res = run(tool.execute({"server": "nope", "tool": "t"}, ctx))
    assert res.status == "error"
    assert "unknown MCP server" in res.error


def test_confirmation_policy_per_server():
    tool = McpCall()
    ctx = _ctx({
        "gated": {"command": "x"},                          # default: confirm
        "trusted": {"url": "http://x/mcp", "confirm": False},
    })
    assert tool.needs_confirmation({"server": "gated"}, ctx) is True
    assert tool.needs_confirmation({"server": "trusted"}, ctx) is False


def test_call_server_error_propagates(monkeypatch):
    async def boom(config, name, tool, arguments, timeout):
        raise client.McpError("MCP tool 'x' failed: boom")
    monkeypatch.setattr(client, "call_tool", boom)
    res = run(McpCall().execute({"server": "fs", "tool": "x"},
                                _ctx({"fs": {"command": "x"}})))
    assert res.status == "error"
    assert "boom" in res.error


def test_env_scrubbed_for_stdio(monkeypatch):
    """A stdio MCP subprocess must not inherit orchestrator secrets."""
    import tools.mcp.client as c

    captured = {}

    class FakeSession:
        def __init__(self, *a): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def initialize(self): pass
        async def list_tools(self):
            class R: tools = []
            return R()

    class FakeStdio:
        def __init__(self, params): captured["env"] = params.env
        async def __aenter__(self): return (None, None)
        async def __aexit__(self, *a): pass

    class FakeParams:
        def __init__(self, command, args, env):
            self.command, self.args, self.env = command, args, env

    monkeypatch.setattr(c, "_sdk",
                        lambda: (FakeSession, FakeParams, FakeStdio, None))
    monkeypatch.setenv("MY_SECRET_TOKEN", "leakme")
    cfg = {"tools": {"mcp": {"servers": {"fs": {"command": "npx",
                                                "env": {"EXPLICIT": "1"}}}}}}
    run(c.list_tools(cfg, "fs", 5))
    assert "MY_SECRET_TOKEN" not in captured["env"]      # suffix-scrubbed
    assert captured["env"]["EXPLICIT"] == "1"            # explicit env wins
