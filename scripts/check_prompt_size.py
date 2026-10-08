#!/usr/bin/env python3
"""check_prompt_size — prompt word-budget gate (growth discipline 2026-10-08).

The gate prompt only ever grows: every fix adds a clause, nothing removes
one. This gate gives prompts the same discipline as the complexity/mypy
baselines: a committed word budget in tests/prompt-budget.txt (one
`path (N)` line per watched file); CI fails when a file EXCEEDS its budget.
Shrinking always passes — bank diets with --write.

An intentional budget increase must be marked in the commit message with a
`baseline-bump: prompt (<reason>)` line — scripts/check_baseline_bump.py
rejects unmarked bumps in CI.

Usage:
  scripts/check_prompt_size.py           # CI mode: fail on growth
  scripts/check_prompt_size.py --write   # regenerate tests/prompt-budget.txt
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
BUDGET = ROOT / "tests" / "prompt-budget.txt"

# Files under a word budget. Add a path here AND a `path (N)` line in the
# budget file (via --write) to watch more prompts.
WATCHED = ["prompts/orchestrator-gate.md"]


def word_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").split())


def load_budget() -> dict[str, int]:
    out: dict[str, int] = {}
    if not BUDGET.exists():
        sys.exit(f"prompt-budget: {BUDGET} missing — generate it with "
                 "scripts/check_prompt_size.py --write")
    for line in BUDGET.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ident, _, tail = line.rpartition(" (")
        out[ident] = int(tail.rstrip(")"))
    return out


def main() -> int:
    cur = {p: word_count(ROOT / p) for p in WATCHED}
    if "--write" in sys.argv[1:]:
        lines = [f"{p} ({n})" for p, n in sorted(cur.items())]
        BUDGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"prompt-budget: written ({len(lines)} files)")
        print("prompt-budget: NOTE — an increased budget only passes CI when "
              "the commit message contains `baseline-bump: prompt (<reason>)`")
        return 0

    budget = load_budget()
    grew = sorted(f"{p} ({budget.get(p, 0)} → {n})"
                  for p, n in cur.items() if n > budget.get(p, 0))
    if grew:
        for line in grew:
            print(f"prompt-budget: GROWING {line}")
        print("prompt-budget: diet the prompt (fold the new clause into an "
              "existing rule — one in, one out), or accept the growth "
              "deliberately: scripts/check_prompt_size.py --write + a "
              "`baseline-bump: prompt (<reason>)` line in the commit message")
        return 1
    for p, n in sorted(cur.items()):
        if n < budget.get(p, 0):
            print(f"prompt-budget: {p} at {n} < budget {budget[p]} — "
                  "consider scripts/check_prompt_size.py --write")
    print("prompt-budget: OK — "
          + ", ".join(f"{p} {n}/{budget.get(p, 0)}" for p, n in sorted(cur.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
