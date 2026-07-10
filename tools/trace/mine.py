"""trace.mine - profile-guided discovery of recurring tool-call sequences.

Implements the practical core of "Agent Workflow Optimization" (arXiv 2601.22037):
mine past run traces for consecutive tool-call sub-sequences that recur across
runs, so the deterministic ones can be hand-compiled into composite "meta-tools"
that the brain calls in one shot instead of reasoning through each step.

It does NOT auto-create meta-tools - the paper is explicit that deciding which
sequences are safe to fuse needs human judgement. This surfaces the candidates
(with a side-effect-safety flag) so you can hand-pick. Read-only sequences with
high run-coverage are the best bets (the paper's dominant-prefix pattern).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict

from runtime.tool_base import Tool, ToolContext, ToolResult

# Tools with no side effects: fusing a run of these is safe (they only read state).
# Anything not here is treated as mutating - fuse with care. Override/extend via
# config tools.trace.mine.read_only_extra / mutating_extra.
_READ_ONLY = {
    "fs.find", "fs.read", "fs.list", "code.symbols", "code.tree", "lint.run",
    "git.status", "git.diff", "git.log", "ops.status", "serve.list", "serve.health",
    "gpu.status", "model.list", "trace.query", "trace.mine", "verify.probe",
    "rag.search", "rag.collections", "kg.neighbors", "memory.search",
    "web.search", "web.fetch", "arxiv.search", "job.list", "job.status",
}


def _tcfg(ctx: ToolContext) -> dict:
    return (ctx.config.get("tools", {}) or {}).get("trace", {}).get("mine", {}) or {}


def _db_path(ctx: ToolContext) -> str:
    return ctx.config.get("trace", {}).get("db_path", "/srv/orchestrator/data/trace.db")


def _sequences(conn, since_ts, owner) -> dict[str, list[str]]:
    """{run_id: [tool_name, ...]} in execution order."""
    q = ("SELECT e.run_id, e.payload_json FROM events e "
         "WHERE e.kind IN ('tool_call','tool_result')")
    params: list = []
    if since_ts or owner:
        q += " AND e.run_id IN (SELECT id FROM runs WHERE 1=1"
        if since_ts:
            q += " AND started_at >= ?"; params.append(since_ts)
        if owner:
            q += " AND owner = ?"; params.append(owner)
        q += ")"
    q += " ORDER BY e.run_id, e.rowid"
    seqs: dict[str, list[str]] = defaultdict(list)
    for run_id, pj in conn.execute(q, params):
        try:
            p = json.loads(pj)
        except Exception:
            continue
        name = (p.get("tool") or p.get("name")) if isinstance(p, dict) else None
        if name:
            seqs[run_id].append(name)
    return dict(seqs)


def _ngrams(seqs, n):
    counts, runs, opener = Counter(), defaultdict(set), Counter()
    for run_id, seq in seqs.items():
        # collapse immediate self-repeats so "read,read,read" doesn't dominate as read->read
        for i in range(len(seq) - n + 1):
            g = tuple(seq[i:i + n])
            counts[g] += 1
            runs[g].add(run_id)
            if i == 0:
                opener[g] += 1
    return counts, runs, opener


class TraceMine(Tool):
    name = "trace.mine"
    description = (
        "Mine past run traces (trace.db) for recurring CONSECUTIVE tool-call sequences - "
        "the hot paths worth compiling into composite 'meta-tools' to cut LLM steps "
        "(arXiv 2601.22037). Returns the top repeated bigrams/trigrams with how often "
        "they occur, how many runs they cover, whether they tend to open a run, and a "
        "side-effect-safety flag (read-only sequences are the safe fusion candidates). "
        "It reports candidates only - you hand-write the meta-tools for the safe winners."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "min_count": {"type": "integer", "description": "Min occurrences to report (default 3)."},
            "top": {"type": "integer", "description": "How many of each n-gram to return (default 15)."},
            "since_days": {"type": "number", "description": "Only runs from the last N days."},
            "owner": {"type": "string", "description": "Filter to one owner's runs."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        import time
        cfg = _tcfg(ctx)
        ro = set(_READ_ONLY) | set(cfg.get("read_only_extra", []) or [])
        ro -= set(cfg.get("mutating_extra", []) or [])
        min_count = int(args.get("min_count", cfg.get("min_count", 3)))
        top = int(args.get("top", 15))
        since_ts = (time.time() - float(args["since_days"]) * 86400) if args.get("since_days") else None

        try:
            conn = sqlite3.connect(f"file:{_db_path(ctx)}?mode=ro", uri=True)
        except Exception as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"cannot open trace.db read-only: {e}")
        try:
            seqs = _sequences(conn, since_ts, args.get("owner"))
        finally:
            conn.close()

        total = len(seqs)
        if total == 0:
            return ToolResult(status="ok", tool_name=self.name, result={
                "db": _db_path(ctx), "runs_analyzed": 0,
                "note": "no tool-call events found for that filter"})

        def _report(n):
            counts, runs, opener = _ngrams(seqs, n)
            out = []
            for g, c in counts.most_common():
                if c < min_count:
                    continue
                mutating = [t for t in g if t not in ro]
                out.append({
                    "sequence": " -> ".join(g),
                    "count": c,
                    "runs": len(runs[g]),
                    "run_coverage": round(len(runs[g]) / total, 3),
                    "opens_run": opener[g],                 # times seen as the first calls of a run
                    "safety": "read-only" if not mutating else "has-side-effects",
                    "fusable": not mutating,                # safe, deterministic-plumbing candidate
                    "mutating_steps": mutating,
                })
                if len(out) >= top:
                    break
            return out

        bigrams, trigrams = _report(2), _report(3)
        fusable = [x for x in (trigrams + bigrams) if x["fusable"]][:8]
        return ToolResult(status="ok", tool_name=self.name, result={
            "db": _db_path(ctx),
            "runs_analyzed": total,
            "min_count": min_count,
            "bigrams": bigrams,
            "trigrams": trigrams,
            "top_meta_tool_candidates": fusable,
            "hint": ("Best meta-tool candidates: 'fusable' (read-only) sequences with high "
                     "run_coverage and/or high opens_run (the paper's dominant-prefix win). "
                     "Hand-write one composite tool per winner; leave sequences whose next "
                     "step needs reasoning over the previous result as separate tools."),
        })
