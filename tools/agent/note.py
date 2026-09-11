"""note.set — the agent's working-notes ledger.

Long runs lose the thread: the original goal scrolls out of the window and old
tool results get compacted to stubs. `note.set` writes a short, structured
working note — the goal in your own words, key decisions, what's done, what's
left. The note lives in the transcript as the tool call itself; because it is
small it survives compaction (only large tool *results* get stubbed), so the
model can re-read it later in the run to re-orient.

Ledger discipline (the GVS5H pattern): the note REPLACES the previous one and
is also written through to `notes.md` in the run's work_root — state on disk
that the token cap and compaction cannot truncate, and that sub-agents sharing
the work_root (code.delegate, agent.spawn) can read. Rewrite it as you make
progress: fold in new findings, DELETE what is superseded, disproven, or now
obvious — whatever you omit is gone. Keep it curated, not append-only.

(The optional per-turn anchor re-injection — agent.anchor.mode — is off by
default; without it the note is NOT re-pinned to every turn, it just stays in
the transcript like any small tool call.)
"""

from __future__ import annotations

from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult

# On-disk filename inside the run's work_root. Sub-agents inherit the root, so
# this doubles as the shared ledger between brain and delegated specialists.
NOTES_FILENAME = "notes.md"


class NoteSet(Tool):
    name = "note.set"
    description = (
        "Write the working note for this run: the goal in your own words, key "
        "decisions and findings, what's done, what's left. This REPLACES the "
        "previous note and is also saved to notes.md in the workspace, where "
        "compaction cannot lose it and delegated sub-agents can read it. "
        "Curate it on every update: fold in new findings and DELETE anything "
        "superseded, disproven, or now obvious — keep it under ~800 words, "
        "never append-only. Reach for it on any multi-step task."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The full curated note (replaces the previous one): "
                               "goal, decisions, findings, done, remaining. Delete "
                               "whatever no longer matters.",
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
        # Write-through to the workspace ledger. Best-effort: a read-only or
        # missing work_root must not fail the note itself (the in-transcript
        # copy above already landed).
        persisted = None
        work_root = getattr(ctx, "work_root", None)
        if work_root:
            try:
                p = Path(work_root) / NOTES_FILENAME
                p.write_text(text + "\n", encoding="utf-8")
                persisted = str(p)
            except Exception:
                persisted = None
        return ToolResult(status="ok", tool_name=self.name,
                          result={"saved": True, "chars": len(text),
                                  "persisted": persisted})
