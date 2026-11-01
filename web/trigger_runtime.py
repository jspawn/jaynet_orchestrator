"""Observe-only arXiv source polling for recurring schedule mandates.

A would_accept decision is an audit record, never a queue. No agent run, work
item, chat delivery, or arbitrary connector execution is reachable from here.
"""

from __future__ import annotations

import os

from runtime.tool_base import ToolContext
from runtime.trigger_store import TriggerStore, mandate_hash
from web import projects as PJ

OBSERVE_MIN_INTERVAL_S = 15 * 60
OBSERVE_RULE_VERSION = "arxiv-new-version-v1"
OBSERVE_TOOL_SCOPE = ["arxiv.search", "arxiv.get"]
OBSERVE_BUDGET = {"max_iterations": 12, "max_cost_usd": 0.25,
                  "max_total_tokens": 250000}


def observe_mandate(entry: dict) -> dict:
    """Server-controlled fields for an admin-created arXiv watch."""
    return {"mode": "observe", "source": "arxiv",
            "rule_version": OBSERVE_RULE_VERSION, "prompt_version": 1,
            "tool_scope": list(OBSERVE_TOOL_SCOPE), "budget": dict(OBSERVE_BUDGET)}


class ArxivObserver:
    def __init__(self, runner):
        self.runner = runner
        self.store = TriggerStore(runner.runs.db_path)

    def _current(self, entry: dict) -> dict | None:
        current = next((e for e in self.runner.schedules.list()
                        if e.get("id") == entry.get("id")), None)
        if not current or not current.get("enabled") or current.get("manual_disabled"):
            return None
        try:
            same_occurrence = (float(current.get("next_fire"))
                               == float(entry["next_fire"]))
        except (KeyError, TypeError, ValueError):
            return None
        if not same_occurrence or mandate_hash(current) != mandate_hash(entry):
            return None
        return current

    def _budget_problem(self, entry: dict) -> str | None:
        budget = entry.get("budget")
        if not isinstance(budget, dict) or set(budget) != set(OBSERVE_BUDGET):
            return "invalid observe budget"
        ceilings = dict(self.runner.runtime.config.get("budgets") or {})
        ceilings.update(self.runner.config.get("budget") or {})
        for key, raw in budget.items():
            try:
                value, ceiling = float(raw), float(ceilings.get(key, 0) or 0)
            except (TypeError, ValueError):
                return f"invalid observe budget {key}"
            if value <= 0 or (ceiling > 0 and value > ceiling):
                return f"observe budget {key} exceeds current ceiling"
        return None

    def _validate(self, entry: dict) -> str | None:
        from web.scheduler_runtime import _schedule_owner_revoked

        if (entry.get("mode") != "observe" or entry.get("source") != "arxiv"
                or entry.get("kind") != "every"
                or entry.get("rule_version") != OBSERVE_RULE_VERSION
                or entry.get("prompt_version") != 1):
            return "invalid observe mandate"
        try:
            if int(entry.get("every_s", 0)) < OBSERVE_MIN_INTERVAL_S:
                return "observe interval below 15 minutes"
        except (TypeError, ValueError):
            return "invalid observe interval"
        query = entry.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            return "invalid arXiv query"
        owner = entry.get("owner")
        account = self.runner.users.get(owner) if owner else None
        if account is None:
            return "owner no longer exists"
        planted = entry.get("created_by")
        if (not (planted and planted != owner)
                and _schedule_owner_revoked(self.runner.users, owner)):
            return "schedules revoked"
        pid = entry.get("project_id")
        if pid and PJ.files_root(self.runner.projects_dir, owner,
                                os.path.basename(str(pid))) is None:
            return "project no longer exists"
        disabled = set(self.runner.users.get_global_disabled_tools())
        scope = entry.get("tool_scope")
        if not isinstance(scope, list) or not scope:
            return "invalid observe tool scope"
        registry = getattr(self.runner.runtime, "registry", None)
        for name in scope:
            tool = registry.get(name) if registry is not None else None
            if (tool is None or not getattr(tool, "read_only", False)
                    or name in disabled
                    or (getattr(tool, "admin_only", False)
                        and not account.get("is_admin"))):
                return f"observe tool unavailable: {name}"
        if "arxiv.search" not in scope:
            return "observe source tool missing"
        return self._budget_problem(entry)

    async def poll(self, entry: dict) -> dict:
        """Fetch and record one occurrence; return whether recurrence may advance."""
        if problem := self._validate(entry):
            return {"advance": True, "status": "error", "error": problem}
        sid = str(entry["id"])
        owner = entry["owner"]
        ctx = ToolContext(request_id=f"observe-{sid}",
                          config=self.runner.runtime.config,
                          budget=None,  # type: ignore[arg-type]  # read-only poll, no run budget
                          owner=owner,
                          is_admin=bool(self.runner.users.get(owner).get("is_admin")),
                          project_id=entry.get("project_id"))
        tool = self.runner.runtime.registry.get("arxiv.search")
        result = await tool.execute(
            {"query": entry["query"], "max_results": 20,
             "sort_by": "lastUpdatedDate", "sort_order": "descending"}, ctx)
        # A pause/delete/rule change during the network call wins: record nothing.
        if self._current(entry) is None:
            return {"advance": False, "status": "cancelled"}
        if problem := self._validate(entry):
            return {"advance": True, "status": "error", "error": problem}
        if result.status != "ok":
            return {"advance": True, "status": "error",
                    "error": result.error or "arXiv poll failed"}
        papers = (result.result or {}).get("papers")
        if not isinstance(papers, list):
            return {"advance": True, "status": "error",
                    "error": "malformed arXiv response"}
        try:
            recorded = self.store.record_poll(entry, papers, entry["next_fire"])
        except Exception as exc:
            return {"advance": True, "status": "error",
                    "error": f"could not record observation: {exc}"}
        return {"advance": True, "status": "ok", "recorded": recorded}
