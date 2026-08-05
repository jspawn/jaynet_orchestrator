"""note.set — the agent's in-transcript progress scratchpad.

Long runs lose the thread: the original goal scrolls out of the window and old
tool results get compacted to stubs. `note.set` writes a short, structured
working note — the goal in your own words, key decisions, what's done, what's
left. The note lives in the transcript as the tool call itself; because it is
small it survives compaction (only large tool *results* get stubbed), so the
model can re-read it later in the run to re-orient. Overwrite it as you make
progress; keep it tight (a few lines), not a transcript.

(The optional per-turn anchor re-injection — agent.anchor.mode — is off by
default; without it the note is NOT re-pinned to every turn, it just stays in
the transcript like any small tool call.)
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult


class NoteSet(Tool):
    name = "note.set"
    description = (
        "Write or replace a short checkpoint note: the goal in your own words, "
        "key decisions, what's done, what's left. It stays in the conversation "
        "and, being small, survives transcript compaction — re-read it later in "
        "a long run to re-orient. Overwrite it as you go and keep it to a few "
        "lines. Reach for it on any multi-step task."
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
