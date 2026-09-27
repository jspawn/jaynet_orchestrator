#!/usr/bin/env python3
"""Run timing breakdown: model time vs tool time vs harness overhead.

Per run (trace.db + runs table):
  wall     = finished_at - started_at
  model    = Σ (model_turn.ts - model_start.ts), per served model alias
  tools    = Σ tool_result.latency_ms, per tool
  harness  = wall - model - tools  (loop logic, prompt builds, gates, SSE —
             the part that is neither the LLM nor a tool)

Flags likely timeouts/sinks: tool calls above WARN_TOOL_S, model spans above
WARN_MODEL_S, plus the harness's own distress events (stall_check,
stall_hard_stop, context_warning, model_turn_capped/truncated).

Usage:
  .venv/bin/python scripts/run_timing.py [--since "2026-09-27 10:00"]
      [--run-id ID] [--limit N] [--db /srv/data/trace.db]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict

WARN_TOOL_S = 60.0
WARN_MODEL_S = 300.0

MODEL_PAIR = {"model_start": "open", "model_turn": "close"}
DISTRESS = ("stall_check", "stall_hard_stop", "context_warning",
            "model_turn_capped", "model_turn_truncated")


def runs_since(db, since_ts, limit):
    cur = db.execute(
        "SELECT id, started_at, finished_at, status, substr(user_message,1,60) "
        "FROM runs WHERE started_at > ? ORDER BY started_at DESC LIMIT ?",
        (since_ts, limit * 4))
    rows = cur.fetchall()
    # keep only runs that have events (skip ping/health noise)
    out = []
    for rid, st, fin, status, msg in rows:
        n = db.execute("SELECT count(*) FROM events WHERE run_id=?",
                       (rid,)).fetchone()[0]
        if n > 2:
            out.append((rid, st, fin, status, msg))
        if len(out) >= limit:
            break
    return out


def analyze(db, rid, started, finished):
    evs = db.execute(
        "SELECT ts, kind, payload_json FROM events WHERE run_id=? ORDER BY ts",
        (rid,)).fetchall()
    model_s = defaultdict(float)      # alias -> seconds
    tool_s = defaultdict(float)       # tool -> seconds
    tool_n = defaultdict(int)
    distress = defaultdict(int)
    open_span = None                  # (alias, ts)
    slow_tools = []                   # (secs, tool, args-preview)
    slow_model = []                   # (secs, alias)
    last_ts = started
    for ts, kind, payload in evs:
        last_ts = max(last_ts, ts)
        if kind == "model_start":
            try:
                alias = json.loads(payload or "{}").get("model") or "?"
            except Exception:
                alias = "?"
            if open_span:             # nested/aborted span: close at this ts
                a, t0 = open_span
                model_s[a] += ts - t0
            open_span = (alias, ts)
        elif kind in ("model_turn", "model_turn_capped",
                      "model_turn_truncated"):
            if open_span:
                a, t0 = open_span
                dt = ts - t0
                model_s[a] += dt
                if dt > WARN_MODEL_S:
                    slow_model.append((dt, a))
                open_span = None
        elif kind == "tool_result":
            try:
                d = json.loads(payload or "{}")
            except Exception:
                continue
            sec = float(d.get("latency_ms") or 0) / 1000.0
            tool = str(d.get("tool") or "?")
            tool_s[tool] += sec
            tool_n[tool] += 1
            if sec > WARN_TOOL_S:
                slow_tools.append((sec, tool,
                                   str(d.get("args"))[:60]))
        elif kind in DISTRESS:
            distress[kind] += 1
    end = finished or last_ts
    if open_span:                     # run ended mid-call (zombie/aborted)
        a, t0 = open_span
        model_s[a] += max(0.0, end - t0)
    wall = max(0.0, end - started)
    m = sum(model_s.values())
    t = sum(tool_s.values())
    return {
        "wall": wall, "model": dict(model_s), "model_s": m,
        "tools": dict(tool_s), "tool_s": t, "tool_n": dict(tool_n),
        "harness": max(0.0, wall - m - t),
        "slow_tools": sorted(slow_tools, reverse=True),
        "slow_model": sorted(slow_model, reverse=True),
        "distress": dict(distress),
    }


def fmt_s(v):
    if v >= 3600:
        return f"{v/3600:.1f}h"
    if v >= 60:
        return f"{v/60:.1f}m"
    return f"{v:.0f}s"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default=time.strftime("%Y-%m-%d 00:00"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--db", default="/srv/data/trace.db")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    if args.run_id:
        row = db.execute(
            "SELECT id, started_at, finished_at, status, "
            "substr(user_message,1,60) FROM runs WHERE id=?",
            (args.run_id,)).fetchone()
        if not row:
            ap.error(f"run {args.run_id} not found")
        runs = [tuple(row)]
    else:
        since_ts = time.mktime(time.strptime(args.since, "%Y-%m-%d %H:%M"))
        runs = runs_since(db, since_ts, args.limit)

    tot = {"wall": 0.0, "model": 0.0, "tools": 0.0, "harness": 0.0}
    model_tot = defaultdict(float)
    tool_tot = defaultdict(float)
    print(f"{'run':8s} {'wall':>7s} {'model':>7s} {'tools':>7s} "
          f"{'harness':>8s}  distress  what")
    for rid, st, fin, status, msg in runs:
        a = analyze(db, rid, st, fin)
        d = ",".join(f"{k.replace('_check','').replace('_hard_stop','!').replace('_warning','⚠')}x{v}"
                     for k, v in a["distress"].items()) or "-"
        print(f"{rid[:8]} {fmt_s(a['wall']):>7s} {fmt_s(a['model_s']):>7s} "
              f"{fmt_s(a['tool_s']):>7s} {fmt_s(a['harness']):>8s}  "
              f"{d:10s} {status} {msg!r}")
        for sec, tool, preview in a["slow_tools"][:3]:
            print(f"         slow tool: {tool} {fmt_s(sec)}  {preview}")
        for sec, alias in a["slow_model"][:2]:
            print(f"         slow model: {alias} {fmt_s(sec)}")
        tot["wall"] += a["wall"]
        tot["model"] += a["model_s"]
        tot["tools"] += a["tool_s"]
        tot["harness"] += a["harness"]
        for k, v in a["model"].items():
            model_tot[k] += v
        for k, v in a["tools"].items():
            tool_tot[k] += v

    if len(runs) > 1:
        w = tot["wall"] or 1
        print("\n== aggregate over", len(runs), "runs ==")
        print(f"model   {fmt_s(tot['model']):>8s} {tot['model']/w:5.0%}"
              + "".join(f"\n  {k:20s} {fmt_s(v):>8s} {v/w:5.0%}"
                        for k, v in sorted(model_tot.items(),
                                           key=lambda x: -x[1])))
        print(f"tools   {fmt_s(tot['tools']):>8s} {tot['tools']/w:5.0%}"
              + "".join(f"\n  {k:20s} {fmt_s(v):>8s} {v/w:5.0%}"
                        for k, v in sorted(tool_tot.items(),
                                           key=lambda x: -x[1])[:8]))
        print(f"harness {fmt_s(tot['harness']):>8s} {tot['harness']/w:5.0%}")


if __name__ == "__main__":
    main()
