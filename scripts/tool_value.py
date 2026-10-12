#!/usr/bin/env python3
"""tool-value — CLI wrapper over runtime.tool_value (see its module docstring).

Usage:
  tool-value.py                  # all tools, sorted by calls
  tool-value.py --min-calls 20   # drop the long tail from the table
  tool-value.py --errors         # sort by error rate instead
  tool-value.py --db PATH        # default $JAYNET_DATA/trace.db

Env: JAYNET_DATA (default /srv/data). Legacy ORCH_DATA fallback.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.tool_value import collect, report  # noqa: E402

DATA = os.environ.get("JAYNET_DATA") or os.environ.get("ORCH_DATA", "/srv/data")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.path.join(DATA, "trace.db"))
    ap.add_argument("--min-calls", type=int, default=1)
    ap.add_argument("--errors", action="store_true",
                    help="sort by error rate (combine with --min-calls)")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"tool-value: no trace DB at {args.db} — nothing to measure")
        return 0
    try:
        tools, runs = collect(args.db)
    except sqlite3.Error as e:
        print(f"tool-value: could not read {args.db}: {e}")
        return 1
    if not tools:
        print(f"tool-value: no tool_result events in {args.db} yet")
        return 0
    print(report(tools, runs, args.min_calls, args.errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
