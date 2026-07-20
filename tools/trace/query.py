"""trace.query — read the agent's own history back, for self-correction & debugging.

The trace DB already records every run and event (model_turn / tool_call /
tool_result / error) but nothing exposes it to the agent. This is that window:
read-only, so it can never mutate the trace. Three views via `view`:

- runs     : recent runs with status, timing, tokens, cost.
- events   : the event stream for one run_id (optionally filtered by kind), so the
             agent can see "what did I just do / why did that tool fail".
- failures : recent failed tool_results and error events across runs — the fastest
             way to answer "what went wrong and where".

Opens the DB in SQLite read-only mode (mode=ro) regardless of anything else, and
bounds every result. Private: the trace holds local args/results.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult


def _db_path(ctx: ToolContext) -> str | None:
    return (ctx.config.get("trace", {}) or {}).get("db_path")


def _connect_ro(path: str) -> sqlite3.Connection:
    # mode=ro guarantees we can never write the trace, even by mistake.
    uri = f"file:{Path(path).expanduser().resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _clip(s, n: int):
    if s is None:
        return None
    s = str(s)
    return s if len(s) <= n else s[:n] + "\u2026"


def _ago(ts) -> str | None:
    if not ts:
        return None
    d = max(0, time.time() - float(ts))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= size:
            return f"{d / size:.1f}{unit} ago"
    return f"{int(d)}s ago"


class TraceQuery(Tool):
    name = "trace.query"
    description = (
        "Read your own run history back (read-only): view=runs lists recent runs "
        "with status/cost; view=events replays one run's tool calls and results "
        "(needs run_id); view=failures surfaces recent tool failures across runs. "
        "Use to recover from a mistake mid-task or to debug why a tool failed."
    )
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "view": {"type": "string", "enum": ["runs", "events", "failures"],
                     "default": "runs"},
            "run_id": {"type": "string", "description": "Required for view=events."},
            "kind": {"type": "string", "enum": ["model_turn", "tool_call", "tool_result", "error"],
                     "description": "events: filter to one event kind."},
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            "max_payload_chars": {"type": "integer", "default": 600, "minimum": 80, "maximum": 4000,
                                  "description": "Clip each event payload to keep context lean."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = _db_path(ctx)
        if not path:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="trace.db_path is not configured")
        if not Path(path).expanduser().exists():
            return ToolResult(status="ok", result={"note": "no trace DB yet (no runs recorded)"},
                              tool_name=self.name)
        try:
            conn = _connect_ro(path)
        except sqlite3.Error as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"cannot open trace DB read-only: {e}")

        view = args.get("view", "runs")
        limit = int(args.get("limit", 20))
        clip = int(args.get("max_payload_chars", 600))
        try:
            if view == "runs":
                rows = conn.execute(
                    "SELECT id, started_at, finished_at, status, owner, user_message, "
                    "total_tokens, cost_usd, error FROM runs "
                    "ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
                runs = [{
                    "run_id": r["id"], "status": r["status"], "owner": r["owner"],
                    "started": _ago(r["started_at"]),
                    "duration_s": (round(r["finished_at"] - r["started_at"], 1)
                                   if r["finished_at"] else None),
                    "tokens": r["total_tokens"], "cost_usd": r["cost_usd"],
                    "message": _clip(r["user_message"], 120),
                    "error": _clip(r["error"], 200),
                } for r in rows]
                return ToolResult(status="ok", result={"view": "runs", "count": len(runs),
                                                        "runs": runs}, tool_name=self.name)

            if view == "events":
                run_id = args.get("run_id")
                if not run_id:
                    return ToolResult(status="error", result=None, tool_name=self.name,
                                      error="view=events requires run_id (get one from view=runs)")
                q = "SELECT ts, kind, iteration, payload_json FROM events WHERE run_id=?"
                params = [run_id]
                if args.get("kind"):
                    q += " AND kind=?"
                    params.append(args["kind"])
                q += " ORDER BY id ASC LIMIT ?"
                params.append(limit)
                rows = conn.execute(q, params).fetchall()
                events = []
                for r in rows:
                    payload = r["payload_json"]
                    try:
                        payload = json.loads(payload)
                        if isinstance(payload, dict):
                            payload = {k: _clip(v, clip) if isinstance(v, str) else v
                                       for k, v in payload.items()}
                    except (ValueError, TypeError):
                        payload = _clip(payload, clip)
                    events.append({"kind": r["kind"], "iteration": r["iteration"],
                                   "payload": payload})
                return ToolResult(status="ok", result={
                    "view": "events", "run_id": run_id, "count": len(events),
                    "events": events}, tool_name=self.name)

            # view == failures
            rows = conn.execute(
                "SELECT run_id, ts, kind, iteration, payload_json FROM events "
                "WHERE kind='error' OR (kind='tool_result' AND payload_json LIKE '%\"status\": \"error\"%') "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            fails = []
            for r in rows:
                payload = r["payload_json"]
                try:
                    payload = json.loads(payload)
                except (ValueError, TypeError):
                    pass
                err = None
                if isinstance(payload, dict):
                    err = payload.get("error") or payload.get("result")
                fails.append({"run_id": r["run_id"], "when": _ago(r["ts"]),
                              "kind": r["kind"], "iteration": r["iteration"],
                              "error": _clip(err if err is not None else payload, clip)})
            return ToolResult(status="ok", result={"view": "failures", "count": len(fails),
                                                    "failures": fails}, tool_name=self.name)
        finally:
            conn.close()
