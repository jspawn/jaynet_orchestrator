"""Sub-agents — `agent.spawn`.

Delegate a self-contained subtask to a nested, bounded agent. The child runs its
own loop with its OWN context (its intermediate tool chatter never enters the
parent's conversation — only the distilled result returns), its own trace, and a
sub-budget carved from the parent's remaining allowance. Use it to keep the
parent's context lean on multi-step work, to give a subtask a narrower tool set,
or to run a subtask on a different model.

The heavy lifting (budget carve-out, depth cap, allowlist intersection,
confirmation routing back to the parent) lives in the loop's `ctx.spawn`; this
tool is a thin, well-described front door the brain can call.

Guarantees worth knowing:
- A child can only use tools the PARENT was already allowed — never an escalation.
- A child's confirmation-gated tool (fs.write, git.commit, …) still prompts the
  human, on the parent's stream.
- The child's spend counts against the parent's ceilings.
- Marked private: a sub-agent may have touched private tools, so its result
  can't be forwarded to a remote LLM unless the run allows private sharing.
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.llm.cloud_models import resolve_model_alias, valid_model_names


class AgentSpawn(Tool):
    name = "agent.spawn"
    description = (
        "Delegate a self-contained subtask to a nested sub-agent and get back only "
        "its final result (its intermediate steps stay out of your context). Give a "
        "complete, standalone `task` — the child sees none of this conversation. "
        "Optionally restrict it to a `tools` subset (e.g. ['web.search','web.fetch'] "
        "for a research child; can only narrow, never exceed, your own tools), run it "
        "on a different `model`, or cap its `budget`. Best for multi-step subtasks "
        "(research-then-summarise, a contained code change) where the working detail "
        "shouldn't clutter the main thread. Overkill for a single tool call — just "
        "make that call yourself."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Complete, standalone instruction for the sub-agent. "
                               "It has no access to this conversation, so include all "
                               "needed context and state what to return.",
            },
            "name": {
                "type": "string",
                "description": "Short label for traces/UI, e.g. 'research' or 'coder'.",
            },
            "tools": {
                "type": "array", "items": {"type": "string"},
                "description": "Restrict the child to these tool names (a subset of "
                               "your own). Omit to let it use everything you can.",
            },
            "model": {
                "type": "string",
                "description": "Brain for the child. Accepts a friendly alias "
                               "(haiku, claude, opus, qwen_plus, qwen_max, "
                               "qwen_coder, gemini_flash, gemini_pro) or a litellm "
                               "alias (claude-haiku, claude-sonnet, claude-opus, "
                               "qwen-max, local-coder, …) — both resolve. Omit to "
                               "use the default local brain.",
            },
            "budget": {
                "type": "object",
                "description": "Optional sub-budget caps (max_cost_usd, "
                               "max_iterations, max_total_tokens, max_wall_clock_s); "
                               "each is clamped to your remaining allowance.",
            },
        },
        "required": ["task"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        if ctx.spawn is None:
            return ToolResult(status="error", result=None,
                              error="sub-agents are not available in this runtime")
        task = (args.get("task") or "").strip()
        if not task:
            return ToolResult(status="error", result=None, error="task is required")
        # Normalize the model the same way llm.call does, so 'haiku' / 'opus' /
        # 'qwen_coder' resolve to their litellm aliases here too (the brain often
        # reuses llm.call's short names). Unknown -> a helpful, actionable error.
        model = args.get("model")
        if model:
            resolved = resolve_model_alias(model)
            if resolved is None:
                return ToolResult(status="error", result=None,
                                  error=f"unknown model '{model}'. "
                                        f"valid: {', '.join(valid_model_names())}")
            model = resolved
        child = await ctx.spawn(
            task,
            tools=args.get("tools"),
            model=model,
            name=args.get("name"),
            budget=args.get("budget"),
        )
        # Surface the child's distilled answer + just enough metadata to reason
        # about it. The child's full step-by-step lives in its own trace run.
        result = {
            "agent": args.get("name") or "sub-agent",
            "status": child.get("status"),
            "answer": child.get("answer"),
            "sub_run_id": child.get("run_id"),
            "budget": child.get("budget"),
        }
        if child.get("error"):
            result["error"] = child["error"]
        status = "ok" if child.get("status") == "ok" else "error"
        return ToolResult(status=status, result=result,
                          error=(None if status == "ok"
                                 else child.get("error") or child.get("status")))
