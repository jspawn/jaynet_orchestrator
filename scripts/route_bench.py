#!/usr/bin/env python3
"""Route bench: CLM System One vs the harness keyword router.

Both scorers see the SAME labeled request set and must pick a strength tag
from the live models.strengths registry (+ 'general' = don't delegate).
This measures the route_request hook timing — before any tool ran — so the
keyword side is exactly what routing_nudge.strength_keywords catches today
(security only; a miss means no route = 'general').

Ground truth (labels are by case FAMILY; some tb cases are mislabeled —
noise is symmetric, it hits both scorers):
  gaia-*            → research        tb-*              → coding
  harness cases     → explicit map    tb security-flav. → security (kw hit)
  hand-labeled chat → see HAND_LABELED (incl. keyword traps: "what is a CVE")

Usage:
  .venv/bin/python scripts/route_bench.py [--clm-url http://127.0.0.1:8700]
                                          [--limit N] [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HARNESS_DIR = ROOT / "evals"
CUSTOM_DIR = Path("/srv/data/custom/evals")

GENERAL = "general"

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


def keyword_route(msg: str) -> str:
    """The harness keyword router at request time: security-keyword hit,
    else no route (= general)."""
    low = msg.lower()
    for tag, kws in KEYWORDS.items():
        if any(kw_hit(k, low) for k in kws):
            return tag
    return GENERAL


def clm_route(clm_url: str, msg: str, timeout: float = 60.0):
    """One System One choice over the registry + general. Returns
    (choice, prob, latency_s)."""
    criteria = dict(STRENGTHS)
    criteria[GENERAL] = ("no specialist needed — plain chat, questions, or a "
                         "simple task the orchestrator handles itself")
    body = json.dumps({
        "model": "clm-latest",
        "state": msg[:4000],
        "questions": {"route": {
            "type": "choice",
            "instructions": "Which specialist strength does this request "
                            "most need? Choose 'general' when no specialist "
                            "is needed.",
            "criteria": criteria}},
    }).encode()
    req = urllib.request.Request(
        clm_url.rstrip("/") + "/v1/systemone", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    lat = time.monotonic() - t0
    ans = (data.get("answers") or {}).get("route") or {}
    choice = str(ans.get("choice") or GENERAL)
    prob = float((ans.get("probabilities") or {}).get(choice) or 0.0)
    return choice, prob, lat


def load_cases(limit: int | None):
    """(test_id, prompt, ground_truth) triples."""
    out = []

    def first_user(doc):
        turns = doc.get("turns") or []
        for t in turns:
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
        elif keyword_route(prompt) == "security":
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
    ap.add_argument("--clm-url", default="http://127.0.0.1:8700")
    ap.add_argument("--threshold", type=float, default=0.6,
                    help="clm route_threshold: below it the hook defers "
                         "(counts as 'general' here)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=None, help="write markdown report here")
    args = ap.parse_args()

    cases = load_cases(args.limit)
    print(f"route bench: {len(cases)} labeled requests", file=sys.stderr)

    rows = []  # (tid, truth, kw, clm_raw, prob, clm_thr, latency)
    for n, (tid, prompt, truth) in enumerate(cases, 1):
        kw = keyword_route(prompt)
        try:
            choice, prob, lat = clm_route(args.clm_url, prompt)
        except Exception as e:  # server hiccup = honest defer
            print(f"  [{n}/{len(cases)}] {tid}: CLM ERROR {e}", file=sys.stderr)
            choice, prob, lat = GENERAL, 0.0, 0.0
        thr = GENERAL if (choice == GENERAL or prob < args.threshold) else choice
        rows.append((tid, truth, kw, choice, prob, thr, lat))
        if n % 25 == 0:
            print(f"  [{n}/{len(cases)}]", file=sys.stderr)

    def acc(col):
        hits = sum(1 for r in rows if r[col] == r[1])
        return hits, len(rows), hits / len(rows) if rows else 0.0

    kw_h, n, kw_a = acc(2)
    clm_h, _, clm_a = acc(3)
    thr_h, _, thr_a = acc(5)
    agree = sum(1 for r in rows if r[2] == r[3]) / n if n else 0.0
    lats = [r[6] for r in rows if r[6] > 0]
    over2 = sum(1 for l in lats if l > 2.0) / len(lats) if lats else 0.0

    tags = sorted({r[1] for r in rows})
    per = {}
    for t in tags:
        sub = [r for r in rows if r[1] == t]
        per[t] = (len(sub),
                  sum(1 for r in sub if r[2] == t),
                  sum(1 for r in sub if r[3] == t),
                  sum(1 for r in sub if r[5] == t))

    misses = [r for r in rows if r[3] != r[1]][:20]

    lines = [
        "# CLM route bench — System One vs keyword router", "",
        f"- date: {time.strftime('%Y-%m-%d %H:%M')}",
        f"- clm: {args.clm_url} (encoder Qwen3-8B Q8_0, CPU), threshold {args.threshold}",
        f"- set: {n} requests — shipped harness cases, imported gaia/tb, "
        f"{len(HAND_LABELED)} hand-labeled chat states",
        "- ground truth by case family (labels noisy for tb; noise is symmetric)",
        "- keyword side = routing_nudge.strength_keywords as shipped "
        "(security-only; a miss routes nothing → 'general')", "",
        "## Headline", "",
        "| scorer | top-1 accuracy |",
        "|---|---|",
        f"| keyword router (today) | {kw_h}/{n} = **{kw_a:.1%}** |",
        f"| CLM raw (no threshold) | {clm_h}/{n} = **{clm_a:.1%}** |",
        f"| CLM @ threshold {args.threshold} (as the hook ships) | {thr_h}/{n} = **{thr_a:.1%}** |",
        "",
        f"CLM↔keyword agreement: {agree:.1%}",
        "",
        "## Per-class accuracy (n, keyword, CLM raw, CLM@thr)", "",
        "| ground truth | n | keyword | CLM raw | CLM@thr |",
        "|---|---|---|---|---|",
    ]
    for t in tags:
        c, k, cr, ct = per[t]
        lines.append(f"| {t} | {c} | {k/c:.0%} | {cr/c:.0%} | {ct/c:.0%} |")
    lines += [
        "",
        "## CLM latency (CPU encoder)", "",
        f"- p50 {pct(lats, 50):.2f}s · p95 {pct(lats, 95):.2f}s · "
        f"max {max(lats) if lats else 0:.2f}s",
        f"- over the hook's 2.0 s route_timeout_s: {over2:.1%} of calls "
        "(those defer to keywords live)", "",
        "## Sample CLM misroutes (first 20)", "",
        "| case | truth | CLM said | p |",
        "|---|---|---|---|",
    ]
    for tid, truth, _kw, choice, prob, _t, _l in misses:
        lines.append(f"| {tid} | {truth} | {choice} | {prob:.2f} |")
    report = "\n".join(lines) + "\n"

    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
