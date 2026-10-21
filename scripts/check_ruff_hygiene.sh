#!/usr/bin/env bash
# check_ruff_hygiene.sh — CI ruff gate for B904/S110 with a committed
# baseline (readiness audit Tier 3 #13).
#
# B904 (raise without `from` inside except) and S110 (try/except/pass) are
# deliberately NOT in the main lint select — the tree carries ~100 known
# sites, and the main `ruff check` step must stay green. This script fails
# the build only on NEW hits:
#
#   Mechanism: run ruff with B904,S110 selected, keep the diagnostic lines,
#   and NORMALIZE them by stripping the :LINE:COL positions, leaving
#   "path.py: CODE message". The sorted normalized lines are compared
#   against tests/ruff-hygiene-baseline.txt as a MULTISET (comm): a line
#   occurring more often than in the baseline is a new violation and fails
#   the step. Fewer violations than the baseline always passes (fixes are
#   welcome — refresh with --write to keep the baseline honest).
#   Normalization makes the check immune to line-number drift from
#   unrelated edits; identical diagnostics on different lines of one file
#   are handled multiset-style, like the mypy gate.
#
# Scope matches the main Ruff CI step.
#
# Usage:
#   scripts/check_ruff_hygiene.sh           # fail on new violations (CI mode)
#   scripts/check_ruff_hygiene.sh --write   # regenerate tests/ruff-hygiene-baseline.txt
set -u
export LC_ALL=C   # sort/comm must agree with the committed baseline on any host
cd "$(dirname "$0")/.." || exit

BASELINE=tests/ruff-hygiene-baseline.txt
SCOPE="runtime web tools scripts tests plugins"
if [ -x .venv/bin/ruff ]; then
    RUFF=.venv/bin/ruff
else
    RUFF=ruff
fi
# Fail closed: without this guard a missing ruff yields an empty violation
# set and the comm diff against the baseline passes (audit #24 D6).
if ! command -v "$RUFF" >/dev/null 2>&1; then
    echo "ruff-hygiene: '$RUFF' not found — install requirements-test.txt"
    exit 1
fi

# concise output = one "path:line:col: CODE message" line per diagnostic —
# the parseable format (ruff 0.16's default "full" output puts the position
# on its own "-->" line).
raw="$("$RUFF" check $SCOPE --select B904,S110 --output-format concise 2>&1)"
rc=$?
# ruff exits 0 (clean) or 1 (violations); anything else is a broken run and
# must not read as "zero violations" (fail closed, audit #24 D6).
if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
    printf '%s\n' "$raw" | head -20
    echo "ruff-hygiene: ruff failed to run (exit $rc)"
    exit 1
fi
current="$(printf '%s\n' "$raw" \
    | grep -E '^[^:]+:[0-9]+:[0-9]+: (B904|S110) ' \
    | sed -E 's/^([^:]+):[0-9]+:[0-9]+: /\1: /' | sort || true)"

if [ "${1:-}" = "--write" ]; then
    if [ -n "$current" ]; then
        printf '%s\n' "$current" > "$BASELINE"
    else
        : > "$BASELINE"
    fi
    echo "baseline written: $(grep -c . "$BASELINE") normalized violations"
    echo "ruff-hygiene: NOTE — an increased baseline only passes CI when the commit message contains \`baseline-bump: ruff-hygiene (<reason>)\`"
    exit 0
fi

if [ ! -f "$BASELINE" ]; then
    echo "ruff-hygiene: $BASELINE missing — generate it with scripts/check_ruff_hygiene.sh --write"
    exit 1
fi

new="$(if [ -n "$current" ]; then printf '%s\n' "$current"; fi | comm -13 "$BASELINE" -)"
if [ -n "$new" ]; then
    echo "ruff-hygiene: NEW violations not covered by $BASELINE:"
    printf '%s\n' "$new"
    echo "ruff-hygiene: fix them, or accept them deliberately with scripts/check_ruff_hygiene.sh --write"
    exit 1
fi

have=0; [ -n "$current" ] && have=$(printf '%s\n' "$current" | grep -c .)
base=$(grep -c . "$BASELINE" || true)
if [ "$have" -lt "$base" ]; then
    echo "ruff-hygiene: $have normalized violations < baseline $base — consider scripts/check_ruff_hygiene.sh --write"
fi
echo "ruff-hygiene: OK — $have normalized violations, all covered by the baseline"
