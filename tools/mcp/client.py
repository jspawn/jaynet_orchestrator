"""MCP client plumbing — shared by mcp.list / mcp.call.

Thin bridge to Model Context Protocol servers, so JayNet can use the wider
MCP ecosystem (filesystem servers, GitHub, databases, …) without each becoming
a native tool namespace. Servers are managed in Admin → Tools → MCP servers
(or directly in runtime.yaml):

    tools:
      mcp:
        timeout_s: 30
        servers:
          github:                       # stdio server (a local subprocess)
            command: npx
            args: ["-y", "@modelcontextprotocol/server-github"]
            env: {GITHUB_TOKEN: "..."}  # merged over the SCRUBBED base env
            confirm: true               # per-call human approval (default)
          internal:                     # streamable-HTTP server (LAN/remote)
            url: http://192.168.1.10:8000/mcp
            confirm: false              # trusted + read-only -> no per-call gate

The official `mcp` package is a lazy optional dependency (requirements-tools.txt);
without it the tools return an actionable error instead of breaking discovery.
"""

from __future__ import annotations

import asyncio
import os
import time

from runtime.tool_base import scrub_env

_INSTALL_HINT = (
    "the 'mcp' package is not installed. Install the optional tools extras: "
    ".venv/bin/pip install -r requirements-tools.txt")

# Discovery cache: {server_name: (fetched_at, [tool descriptors])}. Tool lists
# are near-static; refetching on every mcp.list would spawn the server each time.
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL_S = 300


class McpError(Exception):
    """User-actionable MCP failure (config, connection, protocol)."""


def servers(config: dict) -> dict[str, dict]:
    """Configured MCP servers: {name: server_cfg}."""
    return (((config.get("tools") or {}).get("mcp") or {}).get("servers") or {})


def get_server(config: dict, name: str) -> dict:
    cfg = servers(config).get(name)
    if cfg is None:
        known = ", ".join(sorted(servers(config))) or "(none configured)"
        raise McpError(f"unknown MCP server '{name}'. Configured: {known} — "
                       f"servers are managed in Admin → Tools → MCP servers "
                       f"(or runtime.yaml tools.mcp.servers)")
    return cfg


def timeout_s(config: dict, server_cfg: dict) -> float:
    default = (((config.get("tools") or {}).get("mcp") or {}).get("timeout_s", 30))
    return float(server_cfg.get("timeout_s", default))


def _sdk():
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        try:
            # mcp >= 2.0
            from mcp.client.streamable_http import streamable_http_client as http_client
        except ImportError:
            # mcp 1.x
            from mcp.client.streamable_http import streamablehttp_client as http_client
    except ImportError as e:
        raise McpError(_INSTALL_HINT) from e
    return ClientSession, StdioServerParameters, stdio_client, http_client


async def _with_session(server_cfg: dict, timeout: float, fn):
    """Open a session to one configured server and run fn(session)."""
    ClientSession, StdioServerParameters, stdio_client, http_client = _sdk()

    async def _run():
        if server_cfg.get("url"):
            async with http_client(server_cfg["url"]) as streams:
                read, write = streams[0], streams[1]   # 2-tuple (mcp 2.x) or 3-tuple (1.x)
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await fn(session)
        command = server_cfg.get("command")
        if not command:
            raise McpError("MCP server needs either 'url' (HTTP) or 'command' (stdio)")
        # The subprocess inherits the SCRUBBED env (no *_KEY/*_TOKEN/… leaks);
        # the server entry's own env: block is the explicit, audited exception.
        env = scrub_env(dict(os.environ))
        env.update({str(k): str(v) for k, v in (server_cfg.get("env") or {}).items()})
        params = StdioServerParameters(
            command=command,
            args=[str(a) for a in (server_cfg.get("args") or [])],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except TimeoutError as e:
        raise McpError(f"MCP server did not answer within {timeout:.0f}s") from e
    except McpError:
        raise
    except Exception as e:
        raise McpError(f"{type(e).__name__}: {e}") from e


async def list_tools(config: dict, name: str, timeout: float) -> list[dict]:
    """Tool descriptors for one server, cached for _CACHE_TTL_S."""
    cached = _CACHE.get(name)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_S:
        return cached[1]

    async def _list(session):
        res = await session.list_tools()
        return [{"name": t.name,
                 "description": (t.description or "")[:200]}
                for t in res.tools]

    tools = await _with_session(get_server(config, name), timeout, _list)
    _CACHE[name] = (time.monotonic(), tools)
    return tools


async def call_tool(config: dict, name: str, tool: str,
                    arguments: dict, timeout: float) -> str:
    """Call one tool on one server; returns the text content joined."""
    async def _call(session):
        res = await session.call_tool(tool, arguments or {})
        parts = [c.text for c in (res.content or [])
                 if getattr(c, "type", None) == "text"]
        text = "\n".join(parts)
        if getattr(res, "isError", False):
            raise McpError(f"MCP tool '{tool}' failed: {text[:500] or 'unknown error'}")
        return text

    return await _with_session(get_server(config, name), timeout, _call)


def reset_cache() -> None:
    """Drop the discovery cache (tests, admin reloads)."""
    _CACHE.clear()
