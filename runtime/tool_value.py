"""tool_value — per-tool usage telemetry from a trace DB: who earns their keep?

Per tool: calls, error rate, share of runs that used it, and the ok-rate of
those runs vs the runs that didn't. The ok-rate contrast is a VALUE SIGNAL,
not a verdict — correlation, not causation (hard runs call more tools;
failing runs call even more). Read it together with the error rate: high
use + high errors = a lane that needs work; near-zero calls over thousands
of runs = trim candidates for the toolset A/B (docs/playbook.md §6).

Not derivable from the schema: the depth-0 vs child split (tool_result
payloads carry no depth, subagent_start no child run id) — skipped on
purpose rather than guessed.

CLI: scripts/tool_value.py. Admin UI: Studio & Eval → Eval → Tool telemetry.
"""
from __future__ import annotations

import sqlite3


def collect(db_path: str) -> tuple[dict[str, dict], dict[str, str]]:
    """Per-tool {calls, errors, runs:set} + {run_id: status}. Read-only."""
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    tools: dict[str, dict] = {}
    for name, status, run_id in con.execute(
            "SELECT json_extract(payload_json,'$.tool'),"
            "       json_extract(payload_json,'$.status'), run_id"
            "  FROM events WHERE kind='tool_result'"):
        if not name:
            continue
        t = tools.setdefault(name, {"calls": 0, "errors": 0, "runs": set()})
        t["calls"] += 1
        t["errors"] += 1 if status == "error" else 0
        t["runs"].add(run_id)
    runs = dict(con.execute("SELECT id, status FROM runs"))
    con.close()
    return tools, runs


def report(tools: dict[str, dict], runs: dict[str, str],
           min_calls: int, by_errors: bool) -> str:
    n_runs = len(runs)
    ok_by_run = {r: (s == "ok") for r, s in runs.items()}
    rows = []
    for name, t in tools.items():
        used_ok = [ok_by_run[r] for r in t["runs"] if r in ok_by_run]
        not_ok = [v for r, v in ok_by_run.items() if r not in t["runs"]]
        rows.append({
            "tool": name, "calls": t["calls"],
            "err_pct": round(100 * t["errors"] / t["calls"], 1),
            "run_share": round(100 * len(t["runs"]) / n_runs, 1) if n_runs else 0,
            "ok_with": round(100 * sum(used_ok) / len(used_ok)) if used_ok else None,
            "ok_without": (round(100 * sum(not_ok) / len(not_ok))
                           if not_ok else None),
        })
    rows.sort(key=lambda r: -(r["err_pct"] if by_errors else r["calls"]))
    shown = [r for r in rows if r["calls"] >= min_calls]
    tail = [r for r in rows if r["calls"] < min_calls]
    out = [f"tool-value: {n_runs} runs, {sum(t['calls'] for t in tools.values())}"
           f" tool calls, {len(tools)} tools",
           f"{'tool':24} {'calls':>6} {'err%':>6} {'runs%':>6}"
           f" {'ok-with':>8} {'ok-without':>10}"]
    for r in shown:
        out.append(f"{r['tool']:24} {r['calls']:>6} {r['err_pct']:>6}"
                   f" {r['run_share']:>6}"
                   f" {(str(r['ok_with']) + '%') if r['ok_with'] is not None else '-':>8}"
                   f" {(str(r['ok_without']) + '%') if r['ok_without'] is not None else '-':>10}")
    if tail:
        out.append("")
        out.append(f"rarely used (< {min_calls} calls — trim-candidate review,"
                   f" check before cutting: some tools are rare BY DESIGN,"
                   f" e.g. goal.complete):")
        out.append("  " + ", ".join(f"{r['tool']} ({r['calls']})"
                                    for r in sorted(tail, key=lambda r: r["calls"])))
    return "\n".join(out)
