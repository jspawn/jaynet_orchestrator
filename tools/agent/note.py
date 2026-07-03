"""note.set — the agent's durable progress scratchpad.

Long runs lose the thread: the original goal scrolls out of the window and old
tool results get compacted to stubs. `note.set` writes a short, structured
working note — the goal in your own words, key decisions, what's done, what's
left — that the loop pins to EVERY following turn, above compaction and always
current. Overwrite it as you make progress; keep it tight (a few lines), not a
transcript. It's how you stay oriented on multi-step work.
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult


class NoteSet(Tool):
    name = "note.set"
    description = (
        "Write or replace your working progress note — the loop pins it to every "
        "following turn, so it survives context compaction and long runs. Put the "
        "goal in your own words, the key decisions you've made, what's done, and "
        "what's left. Overwrite it as you go and keep it short. Reach for it on any "
        "multi-step task to avoid losing the thread."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The full note (replaces the previous one). A few lines: "
                               "goal, decisions, done, remaining.",
            },
        },
        "required": ["text"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        if getattr(ctx, "set_note", None) is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="progress notes are not available in this runtime")
        text = (args.get("text") or "").strip()
        ctx.set_note(text)
        return ToolResult(status="ok", tool_name=self.name,
                          result={"saved": True, "chars": len(text)})
