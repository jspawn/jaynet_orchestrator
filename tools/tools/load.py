"""Toolset expansion — `tools.load`.

The run's tool set is chosen once at run start (keyword heuristic + core set)
and frozen, because a stable schema prefix is what keeps the prompt cache
hitting. That guess can be wrong: the request pivots, or the keywords miss.
This tool is the bounded escape hatch — the model names the categories (or
exact tools) it is missing, and the loop adds them from the next turn on.

The heavy lifting (cap per run, caller-fixed sets, disabled tools, the schema
rebuild that busts the prompt cache once) lives in the loop; this tool is a
thin front door, same pattern as agent.spawn → ctx.spawn. Each expansion
re-prefills the conversation, so the description steers the model to call it
only when a needed tool is actually missing.
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult


class ToolsLoad(Tool):
    name = "tools.load"
    read_only = True
    description = (
        "Load an additional tool category mid-run when you need a tool that is "
        "NOT in your current set (e.g. you must save a file but have no "
        "fs.write). Pass category names from the tools table (coding, files, "
        "git, research, infra, knowledge, verification, schedule) or exact "
        "tool names. The new tools are usable from your NEXT turn. Limited to "
        "a couple of loads per run, and each load re-reads the conversation — "
        "call it ONLY when a tool you need is missing, never 'just in case'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "namespaces": {
                "type": "array", "items": {"type": "string"},
                "description": "Category names (coding, files, git, research, "
                               "infra, knowledge, verification, schedule) or "
                               "exact tool names (e.g. 'fs.write').",
            },
        },
        "required": ["namespaces"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        req = args.get("namespaces") or []
        if isinstance(req, str):
            req = [req]
        req = [str(s).strip() for s in req if str(s).strip()]
        if not req:
            return ToolResult(status="error", result=None,
                              error="namespaces is required")
        if ctx.expand_tools is None:
            return ToolResult(status="error", result=None,
                              error="tool expansion is not available in this runtime")
        out = await ctx.expand_tools(req)
        ok = out.get("status") == "ok"
        return ToolResult(status="ok" if ok else "error", result=out,
                          error=(None if ok else out.get("error")))
