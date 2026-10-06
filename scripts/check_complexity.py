#!/usr/bin/env python3
"""check_complexity — ruff C901 baseline gate (doc audit 2026-10-05, #12).

C901 (mccabe cyclomatic complexity) is deliberately NOT in the main lint
select — the tree carries grandfathered hotspots, and the main `ruff check`
step must stay green. This script runs ruff with C901 selected and the
ceiling from ruff.toml (lint.mccabe.max-complexity = 25), normalizes every
violation to a position-independent id "path: function (N)", and fails
only on:

- NEW violations — a function id not in tests/complexity-baseline.txt, or
- GROWING ones — the same function id at a higher N than the baseline
  (a grandfathered hotspot must not quietly get worse).

Same multiset/growth semantics as scripts/check_mypy.sh: shrinking always
passes — bank fixes by refreshing the baseline with --write. Position
independence (path + function name, no line numbers) makes the gate immune
to drift from unrelated edits; same-named functions in one file are handled
multiset-style, like the mypy gate.

Usage:
  scripts/check_complexity.py           # CI mode: fail on new/growing
  scripts/check_complexity.py --write   # regenerate tests/complexity-baseline.txt
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
BASELINE = ROOT / "tests" / "complexity-baseline.txt"
SCOPE = ["runtime", "web", "tools", "scripts", "tests", "plugins"]

RUFF = ROOT / ".venv" / "bin" / "ruff"


def current() -> Counter:
    ruff = str(RUFF) if RUFF.exists() else "ruff"
    r = subprocess.run(
        [ruff, "check", *SCOPE, "--select", "C901",
         "--output-format", "json"],
        capture_output=True, text=True, cwd=ROOT)
    if r.returncode not in (0, 1) and not r.stdout.strip():
        # Fail closed (audit #24 D6): a broken ruff run must not read as
        # "zero violations" and pass against the baseline.
        sys.exit(f"complexity: ruff failed to run: {r.stderr.strip()[:300]}")
    out: Counter = Counter()
    for v in json.loads(r.stdout or "[]"):
        # "... `name` is too complex (N > 25)"
        msg = v.get("message", "")
        try:
            name = msg.split("`")[1]
            n = int(msg.rsplit("(", 1)[1].split(" ")[0])
        except (IndexError, ValueError):
            continue
        fn = v.get("filename", "")
        # Ruff's JSON filename shape is version-dependent (relative to cwd
        # in some versions, absolute in others) — normalize to a repo-
        # relative path so the baseline survives both (CI hit the absolute
        # flavor on a fresh runner while local dev emitted relative).
        p = Path(fn)
        if p.is_absolute():
            try:
                fn = str(p.relative_to(ROOT))
            except ValueError:
                pass  # outside the tree — keep the absolute path
        out[f"{fn}: {name}"] = max(out[f"{fn}: {name}"], n)
    return out


def load_baseline() -> Counter:
    out: Counter = Counter()
    if not BASELINE.exists():
        sys.exit(f"complexity: {BASELINE} missing — generate it with "
                 "scripts/check_complexity.py --write")
    for line in BASELINE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        ident, _, tail = line.rpartition(" (")
        out[ident] = int(tail.rstrip(")"))
    return out


def main() -> int:
    cur = current()
    if "--write" in sys.argv[1:]:
        lines = [f"{ident} ({n})" for ident, n in sorted(cur.items())]
        BASELINE.write_text("\n".join(lines) + ("\n" if lines else ""))
        print(f"baseline written: {len(lines)} grandfathered violations")
        return 0

    base = load_baseline()
    new = sorted(ident for ident in cur if ident not in base)
    grew = sorted(f"{ident} ({base[ident]} → {cur[ident]})"
                  for ident in cur if ident in base and cur[ident] > base[ident])
    if new or grew:
        for ident in new:
            print(f"complexity: NEW violation {ident} ({cur[ident]})")
        for line in grew:
            print(f"complexity: GROWING violation {line}")
        print("complexity: refactor below the ceiling (25), or accept "
              "deliberately with scripts/check_complexity.py --write")
        return 1
    if len(cur) < len(base):
        print(f"complexity: {len(cur)} violations < baseline {len(base)} — "
              "consider scripts/check_complexity.py --write")
    print(f"complexity: OK — {len(cur)} violations, all covered by the baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
