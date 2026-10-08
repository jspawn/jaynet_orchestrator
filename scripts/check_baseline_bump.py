#!/usr/bin/env python3
"""check_baseline_bump — CI gate: baseline GROWTH needs an explicit marker
(growth discipline 2026-10-08).

The baseline gates (complexity, mypy, prompt word budget) only work if
raising them is a deliberate act. When any watched baseline file changed in
the pushed commit range, this script requires:

  - a `baseline-bump: <what> (<reason>)` line in one of the range's commit
    messages, OR
  - the change to be a pure shrink (lines/idents removed, numbers lowered —
    banking fixes never needs a marker).

Watched files:
  tests/complexity-baseline.txt   "path: func (N)" lines — growth = added
                                  ident or higher N
  tests/prompt-budget.txt         "path (N)" lines — growth = higher N
  tests/mypy-baseline.txt         normalized error lines — growth = any
                                  added line (multiset)

Usage (CI):  BASE=<sha> scripts/check_baseline_bump.py
BASE is the push/PR base; falls back to HEAD~1. Locally it is a no-op
convenience check (run before pushing a baseline change).
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
MARKER = "baseline-bump:"
WATCHED = {
    "tests/complexity-baseline.txt": "paren",
    "tests/prompt-budget.txt": "paren",
    "tests/mypy-baseline.txt": "lines",
}


def paren_map(text: str) -> dict[str, int]:
    """'ident (N)' lines → {ident: N}; blank/# lines skipped."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ident, _, tail = line.rpartition(" (")
        try:
            out[ident] = int(tail.rstrip(")"))
        except ValueError:
            continue
    return out


def growth(kind: str, old: str, new: str) -> list[str]:
    """Human-readable growth entries of new over old ([] = pure shrink)."""
    if kind == "paren":
        o, n = paren_map(old), paren_map(new)
        return sorted(
            [f"added: {i} ({n[i]})" for i in n if i not in o]
            + [f"raised: {i} ({o[i]} → {n[i]})" for i in n
               if i in o and n[i] > o[i]])
    # "lines": any new line occurrence not present in the old multiset
    missing = Counter(x for x in new.splitlines() if x.strip())
    missing.subtract(Counter(x for x in old.splitlines() if x.strip()))
    return sorted(f"added: {line}" for line, c in missing.items() if c > 0)


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       cwd=ROOT)
    if r.returncode != 0:
        sys.exit(f"baseline-bump: git {' '.join(args)} failed: "
                 f"{r.stderr.strip()[:200]}")
    return r.stdout


def main() -> int:
    base = os.environ.get("BASE", "").strip()
    if not base or set(base) == {"0"}:
        r = subprocess.run(["git", "rev-parse", "--verify", "HEAD~1"],
                           capture_output=True, text=True, cwd=ROOT)
        if r.returncode != 0:
            print("baseline-bump: single-commit history — nothing to compare")
            return 0
        base = "HEAD~1"

    changed = [f for f in WATCHED
               if git("diff", "--name-only", f"{base}..HEAD", "--", f).strip()]
    if not changed:
        print("baseline-bump: OK — no baseline files changed")
        return 0

    log = git("log", "--format=%B", f"{base}..HEAD")
    if MARKER in log:
        print(f"baseline-bump: OK — marker found for: {', '.join(changed)}")
        return 0

    problems: list[str] = []
    new_gates: list[str] = []
    for f in changed:
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{base}:{f}"],
            capture_output=True, cwd=ROOT).returncode == 0
        if not exists:
            # Baseline file introduced in this range — a NEW restriction,
            # not a raised ceiling: no marker required.
            new_gates.append(f)
            continue
        old = git("show", f"{base}:{f}")
        new = (ROOT / f).read_text(encoding="utf-8")
        for entry in growth(WATCHED[f], old, new):
            problems.append(f"{f}: {entry}")
    if new_gates:
        print(f"baseline-bump: new baseline gate(s) introduced, no marker "
              f"needed: {', '.join(new_gates)}")
    if not problems:
        print(f"baseline-bump: OK — baselines only shrank "
              f"({', '.join(changed)}), no marker needed")
        return 0
    for p in problems:
        print(f"baseline-bump: GROWTH {p}")
    print(f"baseline-bump: baseline growth must be a deliberate act — add a "
          f"`{MARKER} <what> (<reason>)` line to the commit message "
          f"(e.g. `{MARKER} complexity (loop.run: bounce-cap state)`), or "
          f"revert the baseline change and refactor instead")
    return 1


if __name__ == "__main__":
    sys.exit(main())
