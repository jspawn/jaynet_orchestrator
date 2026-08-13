"""goal.complete / goal.blocked — declare the verdict on an active /goal.

Only meaningful in a goal-supervised run (the /goal feature, web/goals.py):
the loop wires ctx.goal_declare for those runs, and the supervisor reads the
declaration after the run to decide whether the goal is done, blocked, or
needs another turn. On any ordinary run the seam is None and the tools say so.
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult


class GoalCompleteTool(Tool):
    name = "goal.complete"
    description = (
        "Declare the active goal COMPLETE — call only when its 'done when' "
        "criterion is verifiably met (not merely close). Pass a short summary "
        "of the evidence. A judge double-checks the declaration; if it "
        "disagrees you get another turn with its objection."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string",
                        "description": "Why the criterion is met — the concrete "
                                       "evidence, not a restatement of the goal."},
        },
        "required": ["summary"],
    }

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        declare = getattr(context, "goal_declare", None)
        if declare is None:
            return ToolResult(
                status="error", result=None, tool_name=self.name,
                error="No active goal for this run — goal.complete only works "
                      "inside a /goal session.")
        summary = str(args.get("summary") or "").strip()
        if not summary:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="summary is required — state the evidence.")
        declare("complete", summary)
        return ToolResult(status="ok", tool_name=self.name,
                          result="Completion declared. Wrap up your answer now; "
                                 "a judge will verify it against the criterion.")


class GoalBlockedTool(Tool):
    name = "goal.blocked"
    description = (
        "Declare the active goal BLOCKED — you cannot make real progress "
        "(missing input only the user has, a persistent external failure, or a "
        "contradiction in the objective). Pass what's blocking you and what "
        "would unblock it. Prefer this over spinning on a hopeless approach."
    )
    parameters = {
        "type": "object",
        "properties": {
            "reason": {"type": "string",
                       "description": "What's blocking progress and what would "
                                      "unblock it."},
        },
        "required": ["reason"],
    }

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        declare = getattr(context, "goal_declare", None)
        if declare is None:
            return ToolResult(
                status="error", result=None, tool_name=self.name,
                error="No active goal for this run — goal.blocked only works "
                      "inside a /goal session.")
        reason = str(args.get("reason") or "").strip()
        if not reason:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="reason is required — say what's blocking you.")
        declare("blocked", reason)
        return ToolResult(status="ok", tool_name=self.name,
                          result="Blocked declared. Summarize where things stand "
                                 "for the user; the goal pauses here.")
