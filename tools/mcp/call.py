"""mcp.call — invoke a tool on a configured MCP server.

Security posture: an MCP server is arbitrary external code (a local subprocess
or an HTTP endpoint), and its tools may DO things, not just read. So:
- every call is confirmation-gated by default (per-server `confirm: false`
  opts out for trusted, read-only servers);
- results are marked private — they never flow to a cloud LLM unless the run
  allows private sharing;
- stdio servers get the scrubbed environment (no orchestrator secrets leak
  into the subprocess; the server entry's own env: block is the explicit,
  audited exception).
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult

from . import client


class McpCall(Tool):
    name = "mcp.call"
    private = True
    description = (
        "Call a tool on an MCP (Model Context Protocol) server. Discover "
        "servers and their tool names/arguments with mcp.list first. MCP "
        "servers are external code and their tools may change state — calls "
        "need human approval unless the server is configured trusted."
    )
    parameters = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
                "description": "Configured server name (see mcp.list).",
            },
            "tool": {
                "type": "string",
                "description": "Tool name on that server (see mcp.list server=...).",
            },
            "arguments": {
                "type": "object",
                "description": "Tool arguments as a JSON object, per the tool's "
                               "schema shown by mcp.list.",
            },
        },
        "required": ["server", "tool"],
    }

    def needs_confirmation(self, args: dict, ctx: ToolContext) -> bool:
        # Per-server policy: confirm defaults ON; a trusted read-only server
        # can opt out in runtime.yaml (tools.mcp.servers.<name>.confirm: false).
        try:
            cfg = client.get_server(ctx.config, args.get("server") or "")
        except client.McpError:
            return True                     # unknown server: gate it anyway
        return bool(cfg.get("confirm", True))

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        server = (args.get("server") or "").strip()
        tool = (args.get("tool") or "").strip()
        if not server or not tool:
            return ToolResult(status="error", result=None,
                              error="server and tool are required")
        arguments = args.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ToolResult(status="error", result=None,
                              error="arguments must be a JSON object")
        try:
            cfg = client.get_server(ctx.config, server)
            text = await client.call_tool(
                ctx.config, server, tool, arguments,
                client.timeout_s(ctx.config, cfg))
        except client.McpError as e:
            return ToolResult(status="error", result=None, error=str(e))
        return ToolResult(status="ok", result=text)
