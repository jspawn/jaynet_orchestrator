"""todos — the agent's visible, structured plan for multi-step work.

One call manages the whole list (the loop owns the state, the UI renders it
live in the ToDos panel):

  set    replace the list with your plan: [{"title": …, "desc": …}, …]
  update id + status (pending/working/done/failed/skipped), and/or note
         (appended to the item — what happened, why skipped), and/or desc
  add    append one item (title, optional desc)
  remove drop an item by id (ids re-number 1..n)
  clear  drop the whole list

Keep exactly one item `working`; when you reach an item, read its desc; when
you finish one, mark it done with a one-line note. The list survives
transcript compaction — the loop re-injects its current state every turn.
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult


class TodosTool(Tool):
    name = "todos"
    description = (
        "Manage a visible step-by-step todo list for this task — the user "
        "watches it live in a side panel. Use it on any multi-step request "
        "(3+ steps): `set` your plan first, then keep exactly one item "
        "'working' and mark each item done/failed/skipped with a short note "
        "as you go. Small one-shot questions don't need a list."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["set", "update", "add", "remove", "clear"],
                       "description": "set = replace the list with your plan; "
                                      "update = change one item; add = append; "
                                      "remove = drop one; clear = drop all."},
            "items": {"type": "array",
                      "items": {"type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "desc": {"type": "string"}},
                                "required": ["title"]},
                      "description": "For set: the full plan, ordered — "
                                     "[{\"title\": …, \"desc\": …}, …]."},
            "id": {"type": "integer",
                   "description": "For update/remove: the item id (1..n)."},
            "status": {"type": "string",
                       "enum": ["pending", "working", "done", "failed",
                                "skipped"],
                       "description": "For update. Only one item may be "
                                      "'working' — setting it un-marks the "
                                      "previous one."},
            "note": {"type": "string",
                     "description": "For update: a short line appended to the "
                                    "item — what happened, or why it was "
                                    "skipped/failed."},
            "title": {"type": "string",
                      "description": "For add: the new item's title."},
            "desc": {"type": "string",
                     "description": "For add/update: what this item involves "
                                    "and how to tell it's done."},
        },
        "required": ["action"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        update = getattr(ctx, "todos_update", None)
        if update is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="todo lists are not available in this runtime")
        res = await update(args if isinstance(args, dict) else {})
        if res.get("status") != "ok":
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=res.get("error") or "todo update failed")
        return ToolResult(status="ok", tool_name=self.name,
                          result={"items": res.get("items") or []})
