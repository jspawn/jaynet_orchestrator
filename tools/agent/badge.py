"""run.badge — a short status label on the run, shown live in the chat UI.

Skills use this to surface which mode is active (the j-space skill badges
`j-space: full` / `j-space: loop` after classifying a task). The label rides
a `badge` SSE event into the run's footer line and the debug view, and is
replayed with saved chats. Nothing is stored server-side beyond the event
itself; the badge is display state, not run state.
"""

from __future__ import annotations

import re

from runtime.tool_base import Tool, ToolContext, ToolResult

_MAX = 40


class RunBadge(Tool):
    name = "run.badge"
    read_only = True
    description = (
        "Set a short status label on this run, shown live in the chat UI "
        "(footer + debug view) — e.g. which skill pass is active: "
        "'j-space: loop'. One line, max 40 chars. Pass an empty label to "
        "clear it. Use sparingly: at most when the mode actually changes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "label": {"type": "string",
                      "description": "The badge text (max 40 chars), or empty to clear."},
        },
        "required": ["label"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        raw = args.get("label")
        label = re.sub(r"\s+", " ", str(raw or "")).strip()[:_MAX]
        emit = getattr(ctx, "emit", None)
        if emit is not None:
            await emit("badge", {"label": label})
        return ToolResult(status="ok", result={"label": label,
                                               "displayed": emit is not None})
