"""eval.run / eval.list / eval.report — the agent can self-test.

eval.run executes eval cases (YAML scenarios in evals/ + the custom layer)
through the real agent loop and grades them with a judge model — the
behavioural complement to the unit test suite. It spends money (a cloud judge
by default) and runs many turns, so it is confirmation-gated; eval.list and
eval.report are read-only views over the cases and eval.db results.

The tools need the live AgentRuntime, which only exists in the web server
process (runtime/eval_runner.set_runtime). On a bare CLI they report that.
"""

from __future__ import annotations

from runtime import eval_runner
from runtime.eval_cases import get_case, load_cases
from runtime.eval_store import EvalStore
from runtime.tool_base import Tool, ToolContext, ToolResult


def _store(ctx: ToolContext) -> EvalStore:
    from runtime import paths
    return EvalStore(paths.EVAL_DB)


def _no_runtime() -> ToolResult:
    return ToolResult(status="error", result=None,
                      error="eval runner is only available in the web server "
                            "process (no live AgentRuntime here)")


class EvalRun(Tool):
    name = "eval.run"
    description = (
        "Run behavioural eval test cases through the real agent loop and grade "
        "them with a judge model. Pass a case 'id' for one test or a 'tag' to "
        "bulk-run every case carrying it. Results + improvement proposals land "
        "in eval.db (see them with eval.report). Costs real money when the "
        "judge is a cloud model — capped by eval.max_cost_usd / "
        "eval.suite_max_cost_usd."
    )
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string",
                   "description": "One case id (from eval.list)."},
            "tag": {"type": "string",
                    "description": "Run every case carrying this tag."},
        },
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        runtime = eval_runner.get_runtime()
        if runtime is None:
            return _no_runtime()
        ecfg = eval_runner.config(ctx.config)
        if not ecfg["enabled"]:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="eval harness is disabled (eval.enabled)")
        case_id = (args.get("id") or "").strip()
        tag = (args.get("tag") or "").strip()
        if bool(case_id) == bool(tag):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="pass exactly one of id (one case) or tag (bulk)")
        if case_id:
            case = get_case(case_id)
            if case is None:
                known = ", ".join(c.id for c in load_cases()) or "none"
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"unknown eval case '{case_id}'. known: {known}")
            cases = [case]
        else:
            cases = [c for c in load_cases() if tag in c.tags]
            if not cases:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"no eval cases tagged '{tag}'")
        store = _store(ctx)
        try:
            summary = await eval_runner.run_suite(runtime, cases, store)
        finally:
            store.close()
        return ToolResult(status="ok", tool_name=self.name, result=summary,
                          cost_usd=summary["cost_usd"])


class EvalList(Tool):
    name = "eval.list"
    description = (
        "List the eval test cases (id, name, tags, driver, origin) with their "
        "latest recorded result, if any."
    )
    read_only = True
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        store = _store(ctx)
        try:
            latest = store.latest_by_test()
        finally:
            store.close()
        out = []
        for c in load_cases():
            row = {"id": c.id, "name": c.name, "tags": c.tags,
                   "driver": c.driver, "origin": c.origin}
            last = latest.get(c.id)
            if last:
                row["last"] = {"passed": bool(last["passed"]),
                               "score": last["score"], "ts": last["ts"]}
            out.append(row)
        return ToolResult(status="ok", tool_name=self.name,
                          result={"count": len(out), "cases": out})


class EvalReport(Tool):
    name = "eval.report"
    description = (
        "Show recorded eval results: pass-rate per case, recent runs, judge "
        "notes. Pass 'id' for one case's trend, omit for an overall summary."
    )
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string",
                   "description": "One case id; omit for the overall summary."},
            "limit": {"type": "integer", "default": 10, "minimum": 1,
                      "maximum": 100},
        },
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        case_id = (args.get("id") or "").strip()
        limit = int(args.get("limit") or 10)
        store = _store(ctx)
        try:
            if case_id:
                rows = store.results(case_id, limit=limit)
                return ToolResult(status="ok", tool_name=self.name, result={
                    "test_id": case_id,
                    "trend": store.trend(case_id, limit=30),
                    "recent": [{k: r[k] for k in
                                ("ts", "passed", "score", "judge_notes",
                                 "cost_usd", "elapsed_s", "status")}
                               for r in rows]})
            rows = store.results(limit=200)
        finally:
            store.close()
        per_case: dict[str, list[dict]] = {}
        for r in rows:
            per_case.setdefault(r["test_id"], []).append(r)
        summary = []
        for tid, rs in sorted(per_case.items()):
            passed = sum(1 for r in rs if r["passed"])
            scores = [r["score"] for r in rs if r["score"] is not None]
            summary.append({
                "test_id": tid, "runs": len(rs),
                "pass_rate": round(passed / len(rs), 3),
                "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
                "last_passed": bool(rs[0]["passed"]),
                "last_ts": rs[0]["ts"]})
        return ToolResult(status="ok", tool_name=self.name, result={
            "total_runs": len(rows), "cases": summary})
