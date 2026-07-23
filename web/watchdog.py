"""Run coroner — automatic post-mortems for distressed runs.

When a run ends badly (stuck / error / stalled, or a non-ok end after heavy
loop-guard churn), the coroner writes a short local-brain analysis into the
ReportStore for the admin area. The same analysis is attached to user-flagged
sessions. Design rules:

- ONE tool-free brain call per report (runtime.complete) — no agent loop,
  no tools, no chance of spiralling. If the brain is down, a raw-facts report
  is stored instead (a distressed run and a dead brain correlate).
- Input is the run's own result dict: status, error, guard stats, budget,
  trajectory — never the user message or the answer text. The trajectory
  carries tool names + call summaries, which is what a diagnosis needs.
- Hard caps: one report per run (ReportStore UNIQUE), max_per_day total.
- Read-only: reports inform the admin; nothing here changes the system.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "min_guard_rejections": 8, "max_per_day": 20}

_TRAJ_CAP = 3000         # trajectory chars handed to the coroner


def config(runtime) -> dict:
    cfg = dict(DEFAULTS)
    raw = (runtime.config.get("watchdog") or {})
    for k in cfg:
        if raw.get(k) is not None:
            cfg[k] = raw[k]
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["min_guard_rejections"] = int(cfg["min_guard_rejections"] or 0)
    cfg["max_per_day"] = int(cfg["max_per_day"] or 0)
    return cfg


def should_report(result: dict, cfg: dict) -> str | None:
    """Trigger reason, or None. Hard failures always; guard churn only on a
    non-ok end (an ok run with rejections is the guard working as designed);
    cancelled runs never."""
    status = result.get("status") or "error"
    if status in ("ok", "cancelled"):
        return None
    if status in ("stuck", "error", "stalled"):
        return status
    rej = int(result.get("guard_rejections") or 0)
    if rej >= cfg["min_guard_rejections"]:
        return f"guard churn ({rej} blocked duplicates)"
    return None


def _facts(result: dict) -> str:
    """The raw-facts block — also the fallback report when the brain is down."""
    b = (result.get("budget") or {})
    t = (b.get("tokens") or {})
    lines = [
        f"status: {result.get('status') or '?'}",
        f"error: {(result.get('error') or '—')[:300]}",
        f"guard rejections: {int(result.get('guard_rejections') or 0)}",
        f"iterations: {b.get('iterations', '?')} · elapsed: "
        f"{b.get('elapsed_s', '?')}s · tokens: {t.get('total', 0):,}",
    ]
    files = result.get("files_changed") or []
    if files:
        lines.append(f"files changed: {', '.join(files[:10])}")
    traj = (result.get("trajectory") or "").strip()
    if traj:
        lines.append(f"trajectory: {traj[:_TRAJ_CAP]}")
    return "\n".join(lines)


async def _coroner(runtime, result: dict) -> str:
    """One tool-free brain call: what happened, likely root cause, one fix.
    Falls back to the raw facts when the brain can't answer."""
    messages = [
        {"role": "system", "content":
         "You are the coroner of a local LLM orchestrator, writing for the "
         "admin. Given a distressed run's facts, answer in ≤120 words, plain "
         "text, three labelled lines:\n"
         "WHAT: one sentence — what the run tried and how it failed.\n"
         "CAUSE: the most likely root cause (vague tool description, missing "
         "context, model behaviour, config/budget, external failure).\n"
         "FIX: one concrete change (prompt wording, tool schema, config "
         "value, or 'none — transient').\n"
         "Base everything on the facts given; say 'unclear' rather than "
         "inventing detail."},
        {"role": "user", "content": _facts(result)},
    ]
    try:
        r = await runtime.complete(messages, think=False)
        text = (r.get("content") or "").strip()
        if not text:
            raise RuntimeError("empty coroner reply")
        return text[:2000]
    except Exception as e:
        log.warning("coroner unavailable, storing raw facts: %s", e)
        return (f"(coroner unavailable: {type(e).__name__} — raw facts)\n"
                + _facts(result))[:2000]


async def maybe_report(runtime, reports, *, run_id: str, owner: str | None,
                       result: dict, trigger: str | None = None) -> dict | None:
    """Write a coroner report for this run if it merits one. Returns the
    stored row, or None (not triggered, deduped, capped, or disabled)."""
    cfg = config(runtime)
    if not cfg["enabled"]:
        return None
    reason = trigger or should_report(result, cfg)
    if not reason:
        return None
    if reports.for_run(run_id):
        return None                                    # one report per run
    if cfg["max_per_day"] and reports.count_today() >= cfg["max_per_day"]:
        log.warning("watchdog: daily report cap reached, skipping run %s", run_id)
        return None
    text = await _coroner(runtime, result)
    return reports.create(
        run_id=run_id, owner=owner or "_token", trigger=reason,
        status=result.get("status") or "?",
        guard_rejections=int(result.get("guard_rejections") or 0),
        report=text)


def result_from_trace(trace_db: str, run_id: str) -> dict | None:
    """Rebuild a result-shaped dict from the trace DB — for runs flagged
    after the fact, where the original result object is long gone. None when
    the run is unknown (or pruned by retention)."""
    if not Path(trace_db).exists():
        return None
    conn = sqlite3.connect(trace_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute(
            "SELECT status, error FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return None
        ev = conn.execute(
            "SELECT payload_json FROM events WHERE run_id=? AND kind='run_finish'"
            " ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
        payload = {}
        if ev:
            try:
                payload = json.loads(ev["payload_json"] or "{}")
            except Exception:
                payload = {}
        return {"status": payload.get("status") or run["status"],
                "error": payload.get("error") or run["error"],
                "trajectory": payload.get("trajectory") or "",
                "budget": payload.get("budget") or {},
                "guard_rejections": payload.get("guard_rejections") or 0}
    finally:
        conn.close()


async def attach_to_flag(runtime, reports, trace_db: str, owner: str,
                         run_ids: list[str]) -> int:
    """Coroner pass over a user flag's runs (max 3). Runs that already have a
    report (e.g. auto-triggered) are skipped; the admin sees them linked via
    the flag detail either way. Returns the number of reports written."""
    written = 0
    for rid in run_ids[:3]:
        if reports.for_run(rid):
            continue
        result = result_from_trace(trace_db, rid)
        if result is None:
            continue
        row = await maybe_report(runtime, reports, run_id=rid, owner=owner,
                                 result=result, trigger="user flag")
        if row:
            written += 1
    return written
