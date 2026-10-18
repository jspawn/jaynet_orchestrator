"""Deterministic tool/skill coverage for self-tests, built from trace events.

The brain is good at choosing probes but bad at remembering a hundred outcomes.
This tool is the scorekeeper: it compares the advertised toolset with the actual
trace and never turns an uncalled or failed tool into a green checkmark.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from runtime import paths
from runtime.skills import discover_skills_layered_cached
from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.skill.load import _skills_dir
from tools.trace.owner import events_clause
from tools.trace.query import _connect_ro, _db_path


def _json_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _result_payload(event: dict) -> dict:
    preview = event.get("result_preview")
    envelope = _json_dict(preview)
    result = envelope.get("result")
    return result if isinstance(result, dict) else {}


def _failure(event: dict) -> str | None:
    if event.get("status") == "error":
        return str(event.get("error") or "unknown tool error")
    result = _result_payload(event)
    if result.get("ok") is False:
        return str(result.get("stderr") or result.get("error")
                   or "tool returned ok=false")
    if result.get("passed") is False:
        return "tool returned passed=false"
    exit_code = result.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return str(result.get("stderr") or f"command exited {exit_code}")
    return None


def _clip(value, limit: int = 400) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _cell(value) -> str:
    return _clip(value).replace("|", "\\|").replace("\n", " ")


def _names(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(name) for name in value if str(name)]


def _score_tools(available: list[str],
                 calls: dict[str, list[dict]]) -> tuple[list[dict], dict[str, int]]:
    report_rows = []
    counts = {"ok": 0, "ok_after_retry": 0, "failed": 0, "not_run": 0}
    for name in sorted(available):
        attempts = calls.get(name, [])
        failures = [_failure(event) for event in attempts]
        failed_indexes = [i for i, failure in enumerate(failures)
                          if failure is not None]
        notes = []
        if not attempts:
            status = "not_run"
            notes.append("available but never called")
        elif failures[-1] is not None:
            status = "failed"
            notes.append(_clip(failures[-1]))
            if len(attempts) > 1:
                notes.append(f"last of {len(attempts)} calls failed")
        else:
            status = "ok_after_retry" if failed_indexes else "ok"
            if failed_indexes:
                earlier = []
                for i in failed_indexes:
                    text = _clip(failures[i], 250)
                    if text not in earlier:
                        earlier.append(text)
                notes.append(f"{len(failed_indexes)} earlier failure(s): "
                             + "; ".join(earlier))
            elif len(attempts) > 1:
                notes.append(f"{len(attempts)} calls")
        if attempts:
            result = _result_payload(attempts[-1])
            if result.get("all_up") is False:
                notes.append("probe worked; stack reported all_up=false")
        counts[status] += 1
        report_rows.append({
            "tool": name, "status": status, "calls": len(attempts),
            "note": "; ".join(notes),
        })
    return report_rows, counts


class TraceCoverage(Tool):
    name = "trace.coverage"
    description = (
        "Generate an evidence-based self-test coverage table from trace.db: "
        "every available tool is marked ok, ok_after_retry, failed, or not_run. "
        "Nested command failures (ok=false, passed=false, nonzero exit_code) "
        "count as failures. Also reports skill discovery and actual skill.load "
        "outcomes. Defaults to the current run; read-only and owner-scoped."
    )
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "run_id": {"type": "string",
                       "description": "Run to score (default: this run)."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = _db_path(ctx)
        if not path:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="trace.db_path is not configured")
        run_id = str(args.get("run_id") or ctx.request_id)
        try:
            conn = _connect_ro(path)
        except sqlite3.Error as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"cannot open trace DB read-only: {e}")

        try:
            frag, params = events_clause(ctx, False)
            rows = conn.execute(
                "SELECT kind, payload_json FROM events WHERE run_id=?" + frag
                + " ORDER BY id ASC", [run_id, *params]).fetchall()
        except sqlite3.Error as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"cannot read trace events: {e}")
        finally:
            conn.close()
        if not rows:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"no trace events found for run {run_id}")

        available: list[str] = []
        seen = set()
        selection_complete = False
        calls: dict[str, list[dict]] = defaultdict(list)
        loaded_ok: set[str] = set()
        load_failed: dict[str, str] = {}

        def add_names(names) -> None:
            for name in _names(names):
                if name not in seen:
                    seen.add(name)
                    available.append(name)

        for kind, raw in rows:
            event = _json_dict(raw)
            if kind == "tool_selection":
                names = event.get("available")
                if not isinstance(names, list):
                    names = event.get("selected")
                if isinstance(names, list):
                    selection_complete = True
                    add_names(names)
                add_names(event.get("added"))
            elif kind == "tool_result":
                name = str(event.get("tool") or "")
                if not name:
                    continue
                calls[name].append(event)
                add_names([name])
                if name == "skill.load":
                    skill = str(_json_dict(event.get("args")).get("name") or "")
                    if event.get("status") == "ok" and skill:
                        loaded_ok.add(skill)
                    elif event.get("status") == "error" and skill:
                        load_failed[skill] = _clip(event.get("error"))

        # This tool's own result is logged only after execute() returns.
        add_names([self.name])
        if self.name not in calls:
            calls[self.name] = [{"status": "ok", "result_preview": None}]

        report_rows, counts = _score_tools(available, calls)

        hidden = getattr(ctx, "disabled_skills", None) or ()
        catalog = [name for name in discover_skills_layered_cached(
            _skills_dir(ctx), paths.CUSTOM_SKILLS_DIR) if name not in hidden]
        skill_list_calls = calls.get("skill.list", [])
        not_loaded = sorted(set(catalog) - loaded_ok)
        skills = {
            "discovered": len(catalog),
            "catalog_checked": bool(skill_list_calls)
                and _failure(skill_list_calls[-1]) is None,
            "loaded_ok": sorted(loaded_ok),
            "load_failed": load_failed,
            "not_loaded": not_loaded,
            "note": ("skill.list proves discovery; loading every skill is not "
                     "required and is deliberately discouraged"),
        }
        summary = {"available": len(available), **counts,
                   "selection_complete": selection_complete}
        markdown = ["| Tool | Result | Note |", "|---|---|---|"]
        markdown.extend(
            f"| `{row['tool']}` | {row['status']} | {_cell(row['note'])} |"
            for row in report_rows)
        markdown.append(
            f"**Skills:** {len(catalog)} discovered; catalog checked: "
            f"{'yes' if skills['catalog_checked'] else 'no'}; "
            f"{len(loaded_ok)} loaded ok; {len(load_failed)} load failures; "
            f"{len(not_loaded)} not loaded (loading all is not required).")
        if load_failed:
            markdown.append("**Skill load failures:** " + "; ".join(
                f"{name}: {_cell(error)}" for name, error in sorted(load_failed.items())))
        markdown.append(
            f"**Summary:** {counts['ok']} ok, {counts['ok_after_retry']} ok after retry, "
            f"{counts['failed']} failed, {counts['not_run']} not run "
            f"({len(available)} available).")
        if not selection_complete:
            markdown.append("Coverage warning: this trace has no concrete initial "
                            "tool-selection list; not_run coverage is incomplete.")
        return ToolResult(status="ok", tool_name=self.name, result={
            "run_id": run_id, "summary": summary, "skills": skills,
            # Do not duplicate report_rows here: on a full-toolset run that
            # pushes the model-facing result beyond ToolResult's 20k cap.
            "markdown": "\n".join(markdown),
            "note": "Use the markdown as the coverage report; do not upgrade "
                    "failed or not_run rows to ok.",
        })
