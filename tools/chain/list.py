"""chain.list — show available pipeline chains."""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult

from . import engine


class ChainList(Tool):
    name = "chain.list"
    read_only = True
    description = (
        "List available chains — named, reusable multi-step pipelines (e.g. "
        "research → distill) defined as YAML files in the chains dir. Run one "
        "with chain.run."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        chains = engine.list_chains(ctx.config)
        if not chains:
            return ToolResult(status="ok", result={
                "chains": [],
                "note": f"no chains found — add YAML files to "
                        f"{engine.chains_dir(ctx.config)}"})
        return ToolResult(status="ok", result={"chains": chains})
