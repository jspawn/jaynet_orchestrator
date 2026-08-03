"""mcp.list — discover configured MCP servers and their tools."""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult

from . import client


class McpList(Tool):
    name = "mcp.list"
    read_only = True
    description = (
        "List configured MCP (Model Context Protocol) servers — external tool "
        "providers bridged into this runtime. Without arguments returns the "
        "server names; pass `server` to also fetch the tools that server "
        "offers (cached for a few minutes). Use mcp.call to invoke one."
    )
    parameters = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
                "description": "Optional. Fetch and show this server's tool list.",
            },
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        cfg = client.servers(ctx.config)
        if not cfg:
            return ToolResult(
                status="ok",
                result={"servers": [],
                        "note": "no MCP servers configured — add them in "
                                "runtime.yaml under tools.mcp.servers"})
        name = (args.get("server") or "").strip()
        if not name:
            return ToolResult(status="ok", result={
                "servers": [
                    {"name": n,
                     "transport": "http" if c.get("url") else "stdio",
                     "confirm": bool(c.get("confirm", True))}
                    for n, c in sorted(cfg.items())
                ],
                "hint": "call mcp.list with server='<name>' to see its tools",
            })
        try:
            server_cfg = client.get_server(ctx.config, name)
            tools = await client.list_tools(
                ctx.config, name, client.timeout_s(ctx.config, server_cfg))
        except client.McpError as e:
            return ToolResult(status="error", result=None, error=str(e))
        return ToolResult(status="ok", result={"server": name, "tools": tools})
