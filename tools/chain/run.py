"""chain.run — execute a named multi-step pipeline.

Steps run sequentially, each seeing the caller's input and prior steps'
outputs via {{placeholders}}. `agent` steps are bounded sub-agents (same
gating and budget carve-out as agent.spawn); `prompt` steps are stateless
LOCAL model calls (cloud is refused — see engine docstring). The first
failing step stops the chain; completed steps are reported for context.
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult

from . import engine


class ChainRun(Tool):
    name = "chain.run"
    # An agent step may have touched private tools; its output must not leak
    # to a cloud LLM unless the run allows private sharing.
    private = True
    description = (
        "Run a named chain — a reusable multi-step pipeline (see chain.list). "
        "Pass the chain `name` and an `input`; each step builds on the "
        "previous step's output and only the final result returns here. "
        "Prefer a chain over hand-orchestrating the same sequence of "
        "agent.spawn/llm calls every time."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Chain name (see chain.list).",
            },
            "input": {
                "type": "string",
                "description": "The chain's input — available to every step as "
                               "{{input}}. Self-contained: steps see none of "
                               "this conversation.",
            },
        },
        "required": ["name", "input"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        input_text = (args.get("input") or "").strip()
        if not input_text:
            return ToolResult(status="error", result=None,
                              error="input is required")
        try:
            chain = engine.load_chain(ctx.config, (args.get("name") or "").strip())
            out = await engine.run_chain(chain, input_text, ctx)
        except engine.ChainError as e:
            return ToolResult(status="error", result=None, error=str(e))
        tokens = out.pop("tokens")
        return ToolResult(
            status="ok",
            result=out,
            # Local models bill $0; the token counts still accrue to the run
            # budget via the loop's tokens_used accounting.
            tokens_used={"model": "chain.local", **tokens},
        )
