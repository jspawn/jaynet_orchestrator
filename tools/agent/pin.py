"""context.pin — protect the most recent tool result from compaction.

Compaction stubs old, large tool results to keep the transcript small, keeping
only the most recent few by default. That's positional: a rare-but-crucial result
from early in the run can be stubbed while recent noise survives. When you get a
result whose FULL text you'll need again later — a schema, a spec, a config, a
long file you're editing against — pin it so it stays verbatim regardless of age.
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult


class ContextPin(Tool):
    name = "context.pin"
    description = (
        "Protect the MOST RECENT tool result from context compaction, so its full "
        "text stays available for the rest of the run instead of being stubbed to "
        "save space. Call it right after a result whose complete output you'll need "
        "again later (a schema, spec, config, or a long file you're working against). "
        "Give a short reason. Pinning is cheap — but don't pin everything, or "
        "compaction can't do its job."
    )
    parameters = {
        "type": "object",
        "properties": {
            "reason": {"type": "string",
                       "description": "Why this result must be kept verbatim (a few words)."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        if getattr(ctx, "pin_last", None) is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="pinning is not available in this runtime")
        info = ctx.pin_last(args.get("reason", ""))
        if not info:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="no tool result to pin yet — call a tool first")
        return ToolResult(status="ok", tool_name=self.name,
                          result={"pinned": info.get("name"), "reason": args.get("reason", "")})
