"""agent.fanout — map/merge: run SEVERAL independent subtasks as parallel children.

The missing parallelization pattern: a task that decomposes into independent
pieces ("summarize each of these 5 files", "audit these 3 modules") currently
serializes in ONE context — every subtask's tool chatter piles into the same
conversation. This tool fans the pieces out as concurrent sub-agents (one
ctx.spawn each, all of spawn's guarantees: narrowed tools, parent ceilings,
confirmation routing) and returns every child's distilled report together, so
the brain merges from envelopes instead of transcripts.

Two honest caveats, inherited from agent.spawn's physics:
- Parallel SPEEDUP only exists across different backing models (brain on one
  card + specialist on another, or cloud children). Children on the same
  local brain share one GPU and run one at a time — the fan-out still buys
  context isolation, just not wall-clock.
- Every child spends against the PARENT's remaining budget: 8 children ×
  their sub-budgets is your ceiling, not 8 new ones.
"""

from __future__ import annotations

import asyncio

from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.agent.spawn import _resolve_spawn_model
from tools.llm.cloud_models import valid_model_names

_MAX_TASKS = 8
_TASK_PREVIEW = 120       # chars of each task echoed back in the result


class AgentFanout(Tool):
    name = "agent.fanout"
    description = (
        "Map/merge: run SEVERAL independent subtasks as parallel sub-agents "
        "and get all their distilled results back together (each child's "
        "working transcript stays in ITS context, never yours). Pass `tasks` "
        "as complete, standalone instructions — children see none of this "
        "conversation. All children share one `tools` subset / `model` / "
        "`strength` routing (same rules as agent.spawn). You merge the "
        "returned reports — that's the reduce step. Use when a task splits "
        "into independent pieces (summarize/audit/transform N things); "
        "overkill for one subtask (use agent.spawn) or one tool call (just "
        "call it). Note: children on the SAME local model serialize on its "
        "GPU — parallel speedup needs different models (specialist/cloud); "
        "context isolation you get either way. All spend counts against "
        "your own budget ceilings."
    )
    private = True

    @property
    def parameters(self) -> dict:
        models = valid_model_names(None)
        return {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array", "items": {"type": "string"},
                    "description": f"Independent subtasks, each a complete, "
                                   f"standalone instruction (max {_MAX_TASKS}). "
                                   "State per task what to return.",
                },
                "name": {
                    "type": "string",
                    "description": "Label prefix for traces/UI (children become "
                                   "'<name>-1', '<name>-2', …). Default 'fanout'.",
                },
                "tools": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Restrict EVERY child to these tool names "
                                   "(a subset of your own). Omit to give each "
                                   "child everything you can use.",
                },
                "model": {
                    "type": "string",
                    "enum": models,
                    "description": "Brain for ALL children (same alias rules "
                                   "as agent.spawn; a live serve.start'd "
                                   "alias also works). Omit for the default "
                                   "local brain. A cloud model sends every "
                                   "child's conversation off-box — same "
                                   "approval/privacy gates as agent.spawn.",
                },
                "strength": {
                    "type": "string",
                    "description": "Route all children by capability tag "
                                   "('coding', …) instead of an explicit "
                                   "model — the harness picks the live "
                                   "specialist carrying the tag. Ignored "
                                   "when `model` is set.",
                },
                "budget": {
                    "type": "object",
                    "description": "Per-child sub-budget caps (max_cost_usd, "
                                   "max_iterations, max_total_tokens, "
                                   "max_wall_clock_s); each clamped to your "
                                   "remaining allowance.",
                },
            },
            "required": ["tasks"],
        }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        if ctx.spawn is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="sub-agents are not available in this runtime")
        raw_tasks = args.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="tasks must be a non-empty array of strings")
        tasks = [str(t).strip() for t in raw_tasks]
        tasks = [t for t in tasks if t]
        if not tasks:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="every task was empty")
        if len(tasks) > _MAX_TASKS:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"too many tasks ({len(tasks)}) — "
                                    f"fan out at most {_MAX_TASKS} children; "
                                    "batch the rest in a second call")

        # Model/strength resolution mirrors agent.spawn exactly (friendly
        # aliases, live serve aliases, strength-tag routing with actionable
        # errors) — one resolution for all children.
        model = args.get("model")
        if model:
            resolved = _resolve_spawn_model(model, ctx)
            if resolved is None:
                return ToolResult(status="error", result=None,
                                  tool_name=self.name,
                                  error=f"unknown model '{model}'. valid: "
                                        f"{', '.join(valid_model_names(ctx.config))}")
            model = resolved
        strength = (args.get("strength") or "").strip()
        if model is None and strength:
            from tools.model.catalog import route_strength
            model = await route_strength(ctx.config, strength)
            if model is None:
                return ToolResult(status="error", result=None,
                                  tool_name=self.name,
                                  error=f"no live specialist tagged "
                                        f"'{strength}' — boot it with "
                                        "model.ensure first, or pass an "
                                        "explicit model")
        base = (args.get("name") or "fanout").strip() or "fanout"

        async def one(i: int, task: str) -> dict:
            entry = {"task": task[:_TASK_PREVIEW], "name": f"{base}-{i + 1}"}
            try:
                child = await ctx.spawn(
                    task,
                    tools=args.get("tools"),
                    model=model,
                    name=entry["name"],
                    budget=args.get("budget"),
                )
            except Exception as e:
                entry.update(status="error",
                             error=f"{type(e).__name__}: {e}")
                return entry
            entry.update(status=child.get("status"),
                         answer=child.get("answer"),
                         files_changed=child.get("files_changed") or [],
                         sub_run_id=child.get("run_id"))
            if child.get("error"):
                entry["error"] = child["error"]
            return entry

        children = await asyncio.gather(*[one(i, t)
                                          for i, t in enumerate(tasks)])
        ok = sum(1 for c in children if c.get("status") == "ok")
        failed = len(children) - ok
        result = {
            "children": children,
            "succeeded": ok,
            "failed": failed,
            "model": model or "(default brain)",
        }
        if strength and model:
            result["routed"] = f"strength '{strength}' → {model}"
        if failed == len(children):
            return ToolResult(status="error", result=result,
                              tool_name=self.name,
                              error="every child failed — see children[].error")
        # Partial failure is signal, not a tool error: the brain merges what
        # succeeded and sees per-child errors inline.
        return ToolResult(status="ok", tool_name=self.name, result=result)
