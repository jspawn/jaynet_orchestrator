"""todos — the agent's visible, structured plan for multi-step work.

One call manages the whole list (the loop owns the state, the UI renders it
live in the ToDos panel):

  set    replace the list with your plan: [{"title": …, "desc": …}, …]
  update id + status (pending/working/done/failed/skipped), and/or note
         (appended to the item — what happened, why skipped), and/or desc,
         and/or check (attach/replace the item's verification command)
  add    append one item (title, optional desc, optional check)
  remove drop an item by id (ids re-number 1..n)
  clear  drop the whole list
  verify re-run an entry's declared check on demand (id/title, or
         requirement="<text>") — for re-verifying after a fix

The separate `requirements` list holds the request's EXPLICIT requirements
(format, spelling, delivery) as tagged strings — replace it wholesale on any
call (or pass it alone, without an action). It is not a plan: each entry is
dropped once verified against the final answer, and an open [must] bounces
the finish once (the loop's requirements gate).

Acceptance ledger: any item or requirement can declare a MACHINE CHECK that
proves it — `<cmd>` via the check field or a "| check: <cmd>" suffix. The
harness runs it sandboxed when the item is marked done (or on `verify`) and
records the outcome: a [must] entry with a check must be VERIFIED, not just
done, before the finish is accepted. `verified` is set by the harness only.

Keep exactly one item `working`; when you reach an item, read its desc; when
you finish one, mark it done with a one-line note. Both lists survive
transcript compaction — the loop re-injects their current state every turn —
and write through to ledger.json in the workspace.
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
        "as you go. Small one-shot questions don't need a list. Explicit "
        "requirements from the request (format, spelling, delivery, "
        "must-contain) go in the separate `requirements` list — plain strings "
        "tagged [must]/[should]/[nice], not todos. Verify every [must] "
        "against your final answer and drop it once met; an open [must] "
        "blocks the finish. Any item or requirement can carry a machine "
        "check ('check' field or '| check: <cmd>' suffix) that PROVES it — "
        "the harness runs it sandboxed when you mark the item done and a "
        "checked [must] must be verified, not just done, to finish."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["set", "update", "add", "remove", "clear",
                                "verify"],
                       "description": "set = replace the list with your plan; "
                                      "update = change one item; add = append; "
                                      "remove = drop one; clear = drop all; "
                                      "verify = re-run an entry's declared "
                                      "check. Omit when only updating "
                                      "requirements."},
            "items": {"type": "array",
                      "items": {"type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "desc": {"type": "string"},
                                    "check": {"type": "string"}},
                                "required": ["title"]},
                      "description": "For set: the full plan, ordered — "
                                     "[{\"title\": …, \"desc\": …, "
                                     "\"check\": \"pytest -q\"}, …]."},
            "requirements": {"type": "array",
                             "items": {"type": "string"},
                             "description": "Replace the requirements list: "
                                            "the request's explicit "
                                            "requirements as plain strings, "
                                            "e.g. '[must] answer spells "
                                            "numbers in words'. Drop each "
                                            "one once verified against the "
                                            "final answer. Add '| check: "
                                            "<cmd>' to make the harness "
                                            "verify it mechanically."},
            "id": {"type": "integer",
                   "description": "For update/remove/verify: the item id "
                                  "(1..n)."},
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
                      "description": "For add: the new item's title. For "
                                     "update/verify: an exact existing title "
                                     "also addresses the item."},
            "desc": {"type": "string",
                     "description": "For add/update: what this item involves "
                                    "and how to tell it's done."},
            "check": {"type": "string",
                      "description": "For add/update: a shell command that "
                                     "PROVES this item (runs sandboxed in "
                                     "the workspace when it is marked done, "
                                     "e.g. 'pytest -q tests/test_x.py'). "
                                     "Empty string clears it."},
            "requirement": {"type": "string",
                            "description": "For verify: the requirement text "
                                           "whose check should (re-)run."},
        },
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
        result = {"items": res.get("items") or []}
        if res.get("requirements"):
            result["requirements"] = res["requirements"]
        if res.get("note"):                   # e.g. plan capped at MAX_ITEMS
            result["note"] = res["note"]
        if res.get("checks_run"):             # ledger: sandboxed check outcomes
            result["checks_run"] = res["checks_run"]
        return ToolResult(status="ok", tool_name=self.name, result=result)
