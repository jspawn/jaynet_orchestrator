#!/usr/bin/env python3
"""eval-peek — inspect JayNet eval runs: status, per-case results, delegation.

Reads the LIVE eval/trace DBs (read-only) and the admin API for run status.

Usage:
  eval-peek.py                     # results of the last 24h + run status
  eval-peek.py --hours 3           # last 3h
  eval-peek.py --since <epoch>     # since a unix timestamp
  eval-peek.py --full              # full judge notes instead of snippets
  eval-peek.py --status            # only the run-status JSON

Env: JAYNET_DATA (default /srv/data), JAYNET_ENV_FILE (default
~/.config/jaynet.env) for JAYNET_WEB_TOKEN, JAYNET_ADMIN (default
http://127.0.0.1:8071).
"""
import argparse
import json
import os
import sqlite3
import time
import urllib.request

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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--since", type=float, default=None,
                    help="unix timestamp; overrides --hours")
    ap.add_argument("--full", action="store_true", help="full judge notes")
    ap.add_argument("--status", action="store_true", help="run status only")
    args = ap.parse_args()

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
    print(f"\ntotal {len(rows)} | passed {npass} ({npass * 100 // len(rows)}%) "
          f"| delegation {ndeleg}/{len(rows)}")


if __name__ == "__main__":
    main()
