#!/usr/bin/env python3
"""eval-peek — inspect JayNet eval runs: status, per-case results, delegation.

Reads the LIVE eval/trace DBs (read-only) and the admin API for run status.

Usage:
  eval-peek.py                     # results of the last 24h + run status
  eval-peek.py --hours 3           # last 3h
  eval-peek.py --since <epoch>     # since a unix timestamp
  eval-peek.py --full              # full judge notes instead of snippets
  eval-peek.py --status            # only the run-status JSON
  eval-peek.py --compare A B       # paired McNemar call between brain labels

Env: JAYNET_DATA (default /srv/data), JAYNET_ENV_FILE (default
~/.config/jaynet.env) for JAYNET_WEB_TOKEN, JAYNET_ADMIN (default
http://127.0.0.1:8071).
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.eval_stats import mcnemar_exact, wilson_interval  # noqa: E402

DATA = os.environ.get("JAYNET_DATA", "/srv/data")
ADMIN = os.environ.get("JAYNET_ADMIN", "http://127.0.0.1:8071")


def token() -> str:
    env = os.environ.get("JAYNET_ENV_FILE",
                         os.path.expanduser("~/.config/jaynet.env"))
    try:
        for line in open(env):
            line = line.strip()
            if line.startswith("JAYNET_WEB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get("JAYNET_WEB_TOKEN", "")


def run_status() -> dict:
    req = urllib.request.Request(
        ADMIN + "/api/admin/evals/run-status",
        headers={"Authorization": "Bearer " + token()})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def _fmt_ci(passes: int, n: int) -> str:
    """' (95% CI 39–72%)' next to a pass rate; '' when there are no runs."""
    ci = wilson_interval(passes, n)
    if ci is None:
        return ""
    return f" (95% CI {ci[0] * 100:.0f}–{ci[1] * 100:.0f}%)"


def _latest_per_case(res: sqlite3.Connection, brain: str) -> dict[str, int]:
    """test_id -> passed of the LATEST non-benchmark result under a brain
    label (the unit a paired comparison is meaningful on)."""
    return {tid: p for tid, p in res.execute(
        "SELECT test_id, passed FROM results r WHERE brain=? AND benchmark=0"
        " AND ts=(SELECT MAX(ts) FROM results WHERE test_id=r.test_id"
        "        AND brain=? AND benchmark=0)", (brain, brain)).fetchall()}


def compare(a: str, b: str) -> None:
    """Paired McNemar call between two brain labels: latest result per case
    under each, exact two-sided test on the discordant pairs. Raw pass
    columns are unpaired single-rep runs whose Wilson intervals overlap
    heavily — THIS is the comparison a bakeoff decision should use."""
    res = sqlite3.connect(f"file:{DATA}/eval.db?mode=ro", uri=True)
    ra, rb = _latest_per_case(res, a), _latest_per_case(res, b)
    paired = sorted(set(ra) & set(rb))
    only_a = sum(1 for t in paired if ra[t] and not rb[t])   # pass only under A
    only_b = sum(1 for t in paired if rb[t] and not ra[t])   # pass only under B
    p = mcnemar_exact(only_a, only_b)
    print(f"compare {a} vs {b}: {len(paired)} paired cases "
          f"({len(ra)} / {len(rb)} recorded)")
    print(f"discordant: b={only_a} (pass only under {a}), "
          f"c={only_b} (pass only under {b})")
    print(f"McNemar exact two-sided p = {p:.4f}")
    discordant = only_a + only_b
    if discordant < 10:
        print(f"-> only {discordant} discordant pairs — the test is "
              "underpowered below 10; treat any outcome as noise, "
              "run more cases/reps before calling this")
    elif p >= 0.05:
        print(f"-> no significant difference between {a} and {b} "
              f"(p = {p:.4f} >= 0.05)")
    else:
        winner = a if only_a > only_b else b
        print(f"-> significant difference (p = {p:.4f} < 0.05): "
              f"{winner} converts more of the discordant cases")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--since", type=float, default=None,
                    help="unix timestamp; overrides --hours")
    ap.add_argument("--full", action="store_true", help="full judge notes")
    ap.add_argument("--status", action="store_true", help="run status only")
    ap.add_argument("--compare", nargs=2, metavar=("BRAIN_A", "BRAIN_B"),
                    help="paired McNemar comparison of two brain labels")
    args = ap.parse_args()

    if args.compare:
        compare(args.compare[0], args.compare[1])
        return

    st = run_status()
    print("run:", "RUNNING" if st.get("running") else "idle",
          "| current:", st.get("current"), "| last:", st.get("last"))
    if args.status or "error" in st:
        return

    since = args.since if args.since is not None else time.time() - args.hours * 3600
    res = sqlite3.connect(f"file:{DATA}/eval.db?mode=ro", uri=True)
    ev = sqlite3.connect(f"file:{DATA}/trace.db?mode=ro", uri=True)
    rows = res.execute(
        "SELECT test_id, passed, run_ids, elapsed_s, judge_notes, ts "
        "FROM results WHERE ts > ? AND benchmark = 0 ORDER BY ts",
        (since,)).fetchall()
    if not rows:
        print("no results in window")
        return

    npass = ndeleg = 0
    for tid, passed, rids, elapsed, notes, ts in rows:
        rids = json.loads(rids or "[]")
        models, ncalls = set(), 0
        if rids:
            qq = ",".join("?" * len(rids))
            models = {r[0] for r in ev.execute(
                "SELECT DISTINCT json_extract(payload_json,'$.model') "
                f"FROM events WHERE run_id IN ({qq})", rids) if r[0]}
            ncalls = ev.execute(
                "SELECT COUNT(*) FROM events "
                f"WHERE run_id IN ({qq}) "
                "AND json_extract(payload_json,'$.tool') IS NOT NULL",
                rids).fetchone()[0]
        other = sorted(m for m in models if m != "local-orchestrator")
        npass += bool(passed)
        ndeleg += bool(other)
        note = (notes or "").replace("\n", " ")
        note = note if args.full else note[:110]
        print(f"{'PASS' if passed else 'fail'}  {tid:<28} "
              f"{int(elapsed or 0):>5}s  {ncalls:>3} calls"
              + (f"  -> {','.join(other)}" if other else ""))
        if note:
            print(f"      {note}")
    print(f"\ntotal {len(rows)} | passed {npass} ({npass * 100 // len(rows)}%"
          f"{_fmt_ci(npass, len(rows))}) "
          f"| delegation {ndeleg}/{len(rows)}")


if __name__ == "__main__":
    main()
