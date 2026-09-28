#!/usr/bin/env python3
"""Route bench: learned decision models vs the harness keyword router.

Every scorer sees the SAME labeled request set and must pick a strength tag
from the live models.strengths registry (+ 'general' = don't delegate).
This measures the route_request hook timing — before any tool ran — so the
keyword side is exactly what routing_nudge.strength_keywords catches today
(security only; a miss means no route = 'general').

Scorers (--scorers, default kw,clm):
  kw   keyword router as shipped (security keywords or nothing)
  clm  CLM System One, local CPU encoder (clm-serve :8700)
  jev  hosted TypeSafe Jev via OpenRouter alpha Decisions API
       (~typesafe/jev-latest; needs $OPENROUTER_API_KEY; request text
       leaves the box — bench data only, never enable live carelessly)
  jevify  local jevify sidecar (open-jev-compatible :8600) — scores with
       whichever model its recipe points at (specialist slot by default)
  julia  Julia-1 (144M mmBERT decision head) via julia-serve on CPU (:8701)

Ground truth (labels are by case FAMILY; some tb cases are mislabeled —
noise is symmetric, it hits all scorers):
  gaia-*            → research        tb-*              → coding
  harness cases     → explicit map    tb security-flav. → security (kw hit)
  hand-labeled chat → see HAND_LABELED (incl. keyword traps: "what is a CVE")

Results cache (--cache): per-case scorer results persist so a new scorer
can be added later without re-running the slow ones (--refresh re-scores).

Usage:
  .venv/bin/python scripts/route_bench.py [--scorers kw,clm,jev]
      [--clm-url http://127.0.0.1:8700] [--cache /tmp/route-bench.json]
      [--limit N] [--refresh] [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HARNESS_DIR = ROOT / "evals"
CUSTOM_DIR = Path("/srv/data/custom/evals")

GENERAL = "general"

JEV_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "~typesafe/jev-latest"

# Live models.strengths registry (config/runtime.yaml) — the hook's criteria.
STRENGTHS = {
    "coding": "code synthesis, debugging, refactors, test fixing",
    "research": "literature and web research, analysis, synthesis",
    "reasoning": "math, logic, planning, tricky deduction",
    "security": "security research, exploit analysis, pentesting",
    "vision": "image understanding",
    "multi-step": "multi-step reasoning chains, puzzles, long sequential computations",
    "creative": "creative and everyday writing — stories, poems, emails, letters, messages, speeches, tone-sensitive rewrites",
}

# Keyword side: tool_selection.routing_nudge.strength_keywords as shipped.
KEYWORDS = {
    "security": ["vulnerability", "vuln", "exploit", "pentest", "pen test",
                 "cve", "sql injection", "xss", "privilege escalation",
                 "malware", "forensic", "security audit", "rce",
                 "reverse shell", "intrusion", "incident response",
                 "security threat", "threat detection", "capture the flag"],
}

# Harness cases: explicit ground truth (everything not listed → general).
HARNESS_LABELS = {
    "code-bugfix": "coding", "code-feature-spec": "coding",
    "code-orientation": "coding", "code-refactor": "coding",
    "code-spec-conflict-trap": "coding", "code-task": "coding",
    "code-weakened-test": "coding", "delegate-coding": "coding",
    "delegate-strength-routing": "coding", "rlm-log-aggregate": "coding",
    "web-fetch-lane": "research", "web-freshness": "research",
    "rlm-notes-sweep": "research",
}

# tb cases whose real demand is sequential reasoning, not code writing.
TB_MULTISTEP = {
    "tb-ancient-puzzle", "tb-blind-maze-explorer-5x5",
    "tb-blind-maze-explorer-algorithm", "tb-chess-best-move",
    "tb-countdown-game",
}

# Hand-labeled chat states — includes keyword traps (security vocabulary in a
# plain question is NOT a security task) and creative/reasoning coverage.
HAND_LABELED = [
    ("What's the capital of France?", GENERAL),
    ("Explain what a CVE is, in one paragraph.", GENERAL),
    ("What does SQL injection mean?", GENERAL),
    ("Translate 'the quick brown fox' into German.", GENERAL),
    ("What is the future value of 21 CHF per month at 6% over 13 years?",
     "reasoning"),
    ("Write a haiku about autumn rain.", "creative"),
    ("Draft a friendly email to my landlord about the broken heater.",
     "creative"),
    ("A bat and a ball cost $1.10 in total. The bat costs $1.00 more than "
     "the ball. How much does the ball cost? Explain.", "reasoning"),
    ("Summarize this week's news about AI chip export rules.", "research"),
    ("Find recent papers on speculative decoding for LLM inference and "
     "compare their reported speedups.", "research"),
    ("Plan a 3-day food trip to Osaka with current restaurant "
     "recommendations.", "research"),
    ("Refactor this Python function to async and add retry logic:\n"
     "def fetch(url):\n    return requests.get(url).json()", "coding"),
    ("My pytest suite fails with a fixture error after upgrading to 8.4 — "
     "help me debug it.", "coding"),
    ("Audit this Flask login endpoint for vulnerabilities:\n"
     "@app.route('/login')\ndef login():\n    q = \"SELECT * FROM users "
     "WHERE name='\" + request.args['u'] + \"'\"", "security"),
    ("How would you pentest a JWT-based API? Give me a checklist.",
     "security"),
    ("Solve the 24 game with 3, 3, 8, 8.", "multi-step"),
]


def kw_hit(kw: str, msg: str) -> bool:
    """Mirror of runtime.loop._strength_kw_hit."""
    if len(kw) <= 4 and " " not in kw:
        return bool(re.search(r"\b" + re.escape(kw) + r"\b", msg))
    return kw in msg


def _criteria() -> dict:
    c = dict(STRENGTHS)
    c[GENERAL] = ("no specialist needed — plain chat, questions, or a "
                  "simple task the orchestrator handles itself")
    return c


def _question() -> dict:
    return {"route": {
        "type": "choice",
        "instructions": "Which specialist strength does this request "
                        "most need? Choose 'general' when no specialist "
                        "is needed.",
        "criteria": _criteria()}}


def score_kw(msg: str, _ctx) -> dict:
    low = msg.lower()
    for tag, kws in KEYWORDS.items():
        if any(kw_hit(k, low) for k in kws):
            return {"choice": tag, "prob": 1.0, "lat": 0.0}
    return {"choice": GENERAL, "prob": 1.0, "lat": 0.0}


def score_clm(msg: str, ctx) -> dict:
    body = json.dumps({"model": "clm-latest", "state": msg[:4000],
                       "questions": _question()}).encode()
    req = urllib.request.Request(
        ctx["clm_url"].rstrip("/") + "/v1/systemone", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    lat = time.monotonic() - t0
    ans = (data.get("answers") or {}).get("route") or {}
    choice = str(ans.get("choice") or GENERAL)
    prob = float((ans.get("probabilities") or {}).get(choice) or 0.0)
    return {"choice": choice, "prob": prob, "lat": round(lat, 3)}


def score_jev(msg: str, ctx) -> dict:
    body = json.dumps({"model": JEV_MODEL, "state": msg[:4000],
                       "questions": _question()}).encode()
    req = urllib.request.Request(
        JEV_ENDPOINT, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + ctx["jev_key"]})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    lat = time.monotonic() - t0
    ans = (data.get("answers") or {}).get("route") or {}
    choice = str(ans.get("choice") or GENERAL)
    prob = float((ans.get("probabilities") or {}).get(choice) or 0.0)
    return {"choice": choice, "prob": prob, "lat": round(lat, 3)}


def score_jevify(msg: str, ctx) -> dict:
    """Local jevify sidecar (open-jev-compatible /v1/systemone) — whichever
    model its recipe points at (specialist slot by default)."""
    body = json.dumps({"model": "open-jev", "state": msg[:4000],
                       "questions": _question()}).encode()
    req = urllib.request.Request(
        ctx["jevify_url"].rstrip("/") + "/v1/systemone", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    lat = time.monotonic() - t0
    ans = (data.get("answers") or {}).get("route") or {}
    choice = str(ans.get("choice") or GENERAL)
    prob = float((ans.get("probabilities") or {}).get(choice) or 0.0)
    return {"choice": choice, "prob": prob, "lat": round(lat, 3)}


def score_julia(msg: str, ctx) -> dict:
    """Julia-1 (144M mmBERT decision head) via julia-serve on CPU."""
    body = json.dumps({"model": "julia-1", "state": msg[:4000],
                       "questions": _question()}).encode()
    req = urllib.request.Request(
        ctx["julia_url"].rstrip("/") + "/v1/systemone", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    lat = time.monotonic() - t0
    ans = (data.get("answers") or {}).get("route") or {}
    choice = str(ans.get("choice") or GENERAL)
    prob = float((ans.get("probabilities") or {}).get(choice) or 0.0)
    return {"choice": choice, "prob": prob, "lat": round(lat, 3)}


SCORERS = {"kw": score_kw, "clm": score_clm, "jev": score_jev,
           "jevify": score_jevify, "julia": score_julia}


def load_cases(limit: int | None):
    """(test_id, prompt, ground_truth) triples."""
    out = []

    def first_user(doc):
        for t in doc.get("turns") or []:
            if isinstance(t, dict) and isinstance(t.get("user"), str):
                return t["user"]
        return ""

    for f in sorted(HARNESS_DIR.glob("*.yaml")):
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        tid = str(doc.get("id") or f.stem)
        prompt = first_user(doc)
        if prompt:
            out.append((tid, prompt, HARNESS_LABELS.get(tid, GENERAL)))
    for f in sorted(CUSTOM_DIR.glob("*.yaml")):
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        tid = str(doc.get("id") or f.stem)
        prompt = first_user(doc)
        if not prompt:
            continue
        if tid.startswith("gaia-"):
            gt = "research"
        elif tid in TB_MULTISTEP:
            gt = "multi-step"
        elif score_kw(prompt, None)["choice"] == "security":
            gt = "security"
        else:
            gt = "coding"
        out.append((tid, prompt, gt))
    for i, (prompt, gt) in enumerate(HAND_LABELED):
        out.append((f"hand-{i:02d}", prompt, gt))
    return out[:limit] if limit else out


def pct(values, q):
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, max(0, round((q / 100) * (len(s) - 1))))
    return s[i]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scorers", default="kw,clm",
                    help="comma list: " + ",".join(SCORERS))
    ap.add_argument("--clm-url", default="http://127.0.0.1:8700")
    ap.add_argument("--jevify-url", default="http://127.0.0.1:8600")
    ap.add_argument("--julia-url", default="http://127.0.0.1:8701")
    ap.add_argument("--threshold", type=float, default=0.6,
                    help="route_threshold: below it the hook defers "
                         "(counts as 'general' here)")
    ap.add_argument("--cache", default="/tmp/route-bench-cache.json")
    ap.add_argument("--refresh", action="store_true",
                    help="re-score even when cached")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=None, help="write markdown report here")
    args = ap.parse_args()

    names = [s.strip() for s in args.scorers.split(",") if s.strip()]
    for n in names:
        if n not in SCORERS:
            ap.error(f"unknown scorer {n!r} (have: {','.join(SCORERS)})")

    ctx = {"clm_url": args.clm_url,
           "jevify_url": args.jevify_url,
           "julia_url": args.julia_url,
           "jev_key": os.environ.get("OPENROUTER_API_KEY", "")}
    if "jev" in names and not ctx["jev_key"]:
        ap.error("scorer jev needs $OPENROUTER_API_KEY")

    cases = load_cases(args.limit)
    cache_path = Path(args.cache)
    cache = {}
    if cache_path.exists() and not args.refresh:
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    print(f"route bench: {len(cases)} labeled requests, "
          f"scorers={names}", file=sys.stderr)
    results = {}  # tid -> {"truth":..., scorer: {"choice","prob","lat"}}
    for n, (tid, prompt, truth) in enumerate(cases, 1):
        row = dict(cache.get(tid) or {})
        row["truth"] = truth
        for name in names:
            if name in row and not args.refresh:
                continue
            try:
                row[name] = SCORERS[name](prompt, ctx)
            except Exception as e:  # server hiccup = honest defer
                print(f"  [{n}/{len(cases)}] {tid} {name} ERROR {e}",
                      file=sys.stderr)
                row[name] = {"choice": GENERAL, "prob": 0.0, "lat": 0.0,
                             "error": str(e)[:120]}
        results[tid] = row
        if n % 25 == 0:
            print(f"  [{n}/{len(cases)}]", file=sys.stderr)
            cache_path.write_text(json.dumps(results), encoding="utf-8")
    cache_path.write_text(json.dumps(results), encoding="utf-8")

    rows = [(tid, r) for tid, r in results.items()]
    n = len(rows)

    def acc(scorer, thr=False):
        hits = 0
        for _tid, r in rows:
            s = r.get(scorer) or {}
            c = str(s.get("choice") or GENERAL)
            p = float(s.get("prob") or 0.0)
            if thr and (c == GENERAL or p < args.threshold):
                c = GENERAL
            if c == r["truth"]:
                hits += 1
        return hits

    tags = sorted({r["truth"] for _t, r in rows})
    learned = [s for s in names if s != "kw"]

    lines = [
        "# Route bench — decision models vs keyword router", "",
        f"- date: {time.strftime('%Y-%m-%d %H:%M')}",
        f"- scorers: {', '.join(names)} · threshold {args.threshold}",
        f"- set: {n} requests — shipped harness cases, imported gaia/tb, "
        f"{len(HAND_LABELED)} hand-labeled chat states",
        "- ground truth by case family (labels noisy for tb; noise is symmetric)",
        "- keyword side = routing_nudge.strength_keywords as shipped "
        "(security-only; a miss routes nothing → 'general')", "",
        "## Headline", "",
        "| scorer | top-1 accuracy |",
        "|---|---|",
    ]
    for s in names:
        h = acc(s)
        label = {"kw": "keyword router (today)", "clm": "CLM raw",
                 "jev": "hosted Jev raw"}.get(s, s)
        lines.append(f"| {label} | {h}/{n} = **{h/n:.1%}** |")
    for s in learned:
        h = acc(s, thr=True)
        label = {"clm": "CLM", "jev": "hosted Jev"}.get(s, s)
        lines.append(f"| {label} @ threshold {args.threshold} (as the hook "
                     f"ships) | {h}/{n} = **{h/n:.1%}** |")
    lines += ["", "## Per-class accuracy", "",
              "| ground truth | n | " + " | ".join(names) + " | "
              + " | ".join(f"{s}@thr" for s in learned) + " |",
              "|---|---|" + "---|" * (len(names) + len(learned))]
    for t in tags:
        sub = [(tid, r) for tid, r in rows if r["truth"] == t]
        c = len(sub)
        cols = []
        for s in names:
            h = sum(1 for _t, r in sub
                    if str((r.get(s) or {}).get("choice") or GENERAL) == t)
            cols.append(f"{h/c:.0%}")
        for s in learned:
            h = 0
            for _t, r in sub:
                d = r.get(s) or {}
                ch = str(d.get("choice") or GENERAL)
                if ch == GENERAL or float(d.get("prob") or 0) < args.threshold:
                    ch = GENERAL
                if ch == t:
                    h += 1
            cols.append(f"{h/c:.0%}")
        lines.append(f"| {t} | {c} | " + " | ".join(cols) + " |")

    for s in learned:
        lats = [float((r.get(s) or {}).get("lat") or 0)
                for _t, r in rows if (r.get(s) or {}).get("lat")]
        if not lats:
            continue
        over2 = sum(1 for l in lats if l > 2.0) / len(lats)
        lines += ["", f"## {s} latency", "",
                  f"- p50 {pct(lats, 50):.2f}s · p95 {pct(lats, 95):.2f}s · "
                  f"max {max(lats):.2f}s",
                  f"- over the hook's 2.0 s route_timeout_s: {over2:.1%} "
                  "(those defer to keywords live)"]
    for s in learned:
        misses = [(tid, r) for tid, r in rows
                  if str((r.get(s) or {}).get("choice") or GENERAL)
                  != r["truth"]][:20]
        lines += ["", f"## Sample {s} misroutes (first 20)", "",
                  "| case | truth | said | p |", "|---|---|---|---|"]
        for tid, r in misses:
            d = r.get(s) or {}
            lines.append(f"| {tid} | {r['truth']} | "
                         f"{d.get('choice')} | {float(d.get('prob') or 0):.2f} |")
    report = "\n".join(lines) + "\n"

    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
