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

Every report ends with the guard telemetry (claude audit 2026-10-06 #6):
per guard, the share of runs it fired in and the pass rate when fired vs
when quiet — the input for the guards_off ablation (a rail whose
pass-after-fire is ~0 only fires on doomed runs).

Env: JAYNET_DATA (default /srv/data), JAYNET_ENV_FILE (default
~/.config/jaynet.env) for JAYNET_WEB_TOKEN, JAYNET_ADMIN (default
http://127.0.0.1:8071). Legacy ORCH_* names are read as a fallback.
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

DATA = os.environ.get("JAYNET_DATA") or os.environ.get("ORCH_DATA", "/srv/data")
ADMIN = os.environ.get("JAYNET_ADMIN", "http://127.0.0.1:8071")


def token() -> str:
    env = os.environ.get("JAYNET_ENV_FILE",
                         os.path.expanduser("~/.config/jaynet.env"))
    try:
        for line in open(env):
            line = line.strip()
            if line.startswith("JAYNET_WEB_TOKEN=") or line.startswith("ORCH_WEB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get("JAYNET_WEB_TOKEN") or os.environ.get("ORCH_WEB_TOKEN", "")


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
    try:
        ev = sqlite3.connect(f"file:{DATA}/trace.db?mode=ro", uri=True)
    except sqlite3.OperationalError:
        ev = None   # no trace data: guard/delegation stats degrade, not crash
        print("note: no trace data (trace.db unreadable) — "
              "guard/delegation stats skipped")
    rows = res.execute(
        "SELECT test_id, passed, run_ids, elapsed_s, judge_notes, ts, status "
        "FROM results WHERE ts > ? AND benchmark = 0 ORDER BY ts",
        (since,)).fetchall()
    if not rows:
        print("no results in window")
        return

    npass = ndeleg = nfanout = nsubcall = ninvalid = 0
    guard_stats: dict[str, list[int]] = {}   # name -> [fires, cases, fired+passed]
    for tid, passed, rids, elapsed, notes, ts, rstatus in rows:
        invalid = rstatus == "invalid"   # fallback-served: unmeasurable
        ninvalid += invalid
        rids = json.loads(rids or "[]")
        tools, ncalls, fires = set(), 0, {}
        if ev is not None and rids:
            qq = ",".join("?" * len(rids))
            tools = {r[0] for r in ev.execute(
                "SELECT DISTINCT json_extract(payload_json,'$.tool') "
                f"FROM events WHERE run_id IN ({qq}) AND kind='tool_result' "
                "AND json_extract(payload_json,'$.status')='ok'", rids) if r[0]}
            ncalls = ev.execute(
                "SELECT COUNT(*) FROM events "
                f"WHERE run_id IN ({qq}) "
                "AND json_extract(payload_json,'$.tool') IS NOT NULL",
                rids).fetchone()[0]
            for name, n in ev.execute(
                    "SELECT json_extract(payload_json,'$.name'), COUNT(*) "
                    f"FROM events WHERE run_id IN ({qq}) "
                    "AND kind='guard_fired' GROUP BY 1", rids):
                if not name:
                    continue
                fires[name] = n
                s = guard_stats.setdefault(name, [0, 0, 0])
                s[0] += n            # a guard can fire several times per case
                s[1] += 1            # cases it touched (fire rate's denominator)
                s[2] += bool(passed)
        specialist = bool(tools & {"specialist.delegate", "code.delegate"})
        fanout = bool(tools & {"agent.spawn", "agent.fanout"})
        subcall = "llm.call" in tools
        npass += bool(passed) and not invalid
        if not invalid:
            ndeleg += specialist
            nfanout += fanout
            nsubcall += subcall
        labels = (["specialist"] if specialist else []) \
            + (["fanout"] if fanout else []) + (["llm.call"] if subcall else [])
        note = (notes or "").replace("\n", " ")
        note = note if args.full else note[:110]
        gtxt = ",".join(f"{k}×{v}" for k, v in sorted(fires.items()))
        print(f"{'INVL' if invalid else 'PASS' if passed else 'fail'}  {tid:<28} "
              f"{int(elapsed or 0):>5}s  {ncalls:>3} calls"
              + (f"  guards: {gtxt}" if gtxt else "")
              + (f"  -> {','.join(labels)}" if labels else ""))
        if note:
            print(f"      {note}")
    counted = len(rows) - ninvalid
    print(f"\ntotal {counted} (+{ninvalid} invalid) | passed {npass} "
          f"({npass * 100 // max(counted, 1)}%"
          f"{_fmt_ci(npass, counted)}) "
          f"| delegation {ndeleg}/{counted} | fanout {nfanout} "
          f"| llm.call {nsubcall}")
    if guard_stats:
        print("\nguards: fires | cases | pass rate fired vs quiet "
              "(pass-after-fire ~0 = the rail only sees doomed runs):")
        for name, (nfires, cases, ok) in sorted(guard_stats.items(),
                                                key=lambda kv: (-kv[1][1], kv[0])):
            quiet = len(rows) - cases
            fired_pct = f"{ok * 100 // cases}%" if cases else "—"
            quiet_pct = f"{(npass - ok) * 100 // quiet}%" if quiet else "—"
            print(f"  {name:<28} {nfires:>3} fires  {cases:>3}/{len(rows)} cases "
                  f"({cases * 100 // len(rows):>2}%)"
                  f"  fired-pass {fired_pct:>4}  quiet-pass {quiet_pct:>4}")
    elif ev is None:
        print("\nguards: no trace data")
    else:
        print("\nguards: no guard fires in window")


if __name__ == "__main__":
    main()
