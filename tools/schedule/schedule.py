"""schedule.* — one-shot reminders and recurring scheduled agent runs.

schedule.add registers a prompt the web server fires later (one-shot at a time,
or recurring on an interval); the result lands in your 'Scheduled runs' chat.
Adding is confirmation-gated (autonomous future spend); listing/removing are
owner-scoped reads/edits of your own entries only.
"""

from __future__ import annotations

import time
from datetime import datetime

from runtime.scheduler import ScheduleStore, parse_every, parse_when
from runtime.tool_base import Tool, ToolContext, ToolResult


def _store(ctx: ToolContext) -> ScheduleStore:
    from runtime import paths
    cfg = (ctx.config.get("tools", {}) or {}).get("schedule", {}) or {}
    return ScheduleStore(cfg.get("store", str(paths.DATA / "schedules.json")))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes")


def _view(e: dict) -> dict:
    return {"id": e.get("id"), "prompt": (e.get("prompt") or "")[:120],
            "kind": e.get("kind"), "enabled": e.get("enabled"),
            "next_fire": _iso(e["next_fire"]) if e.get("next_fire") else None,
            "every_s": e.get("every_s"), "fire_count": e.get("fire_count", 0)}


class ScheduleAdd(Tool):
    name = "schedule.add"
    description = (
        "Schedule a prompt to run later, unattended: one-shot ('run_at': '+30m' "
        "or an ISO datetime) or recurring ('every': '2h', '1d', '1w' …). At fire "
        "time the task runs through the normal agent loop and the result lands "
        "in your 'Scheduled runs' chat. Gated calls auto-approve then (consent "
        "is given HERE, now), so only schedule tasks you'd approve unattended; "
        "the run is still budget-capped."
    )
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string",
                       "description": "The standalone task to run at fire time."},
            "run_at": {"type": "string",
                       "description": "One-shot time: '+30m'/'+2h'/'+1d' or ISO-8601."},
            "every": {"type": "string",
                      "description": "Recurring interval: '30m', '2h', '1d', '1w'."},
        },
        "required": ["prompt"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="prompt is required")
        run_at, every = args.get("run_at"), args.get("every")
        if bool(run_at) == bool(every):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="pass exactly one of run_at (one-shot) or every (recurring)")
        try:
            if every:
                every_s = parse_every(every)
                entry = {"kind": "every", "every_s": every_s,
                         "next_fire": time.time() + every_s}
            else:
                entry = {"kind": "once", "next_fire": parse_when(run_at)}
        except ValueError as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=str(e))
        entry["owner"] = getattr(ctx, "owner", None) or "cli"
        entry["prompt"] = prompt
        saved = _store(ctx).add(entry)
        return ToolResult(status="ok", tool_name=self.name, result={
            "id": saved["id"], "kind": saved["kind"],
            "next_fire": _iso(saved["next_fire"]),
            "note": "fires unattended; watch the 'Scheduled runs' chat for the result"})


class ScheduleList(Tool):
    name = "schedule.list"
    description = "List your scheduled prompts (one-shot and recurring) with next fire times."
    private = True
    read_only = True
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        owner = getattr(ctx, "owner", None) or "cli"
        entries = _store(ctx).list(owner)
        return ToolResult(status="ok", tool_name=self.name, result={
            "count": len(entries), "schedules": [_view(e) for e in entries]})


class ScheduleRemove(Tool):
    name = "schedule.remove"
    description = "Remove one of your scheduled prompts by id (schedule.list shows ids)."
    private = True
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        owner = getattr(ctx, "owner", None) or "cli"
        ok = _store(ctx).remove(args.get("id", ""), owner)
        if not ok:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"no schedule with id {args.get('id')!r} (yours only)")
        return ToolResult(status="ok", tool_name=self.name,
                          result={"removed": args["id"]})
