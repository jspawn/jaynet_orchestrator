#!/usr/bin/env bash
# check_mypy.sh — CI mypy gate with a committed baseline (code audit item 13).
#
# mypy is in requirements-test.txt but was never run in CI, so the tree
# carries a few hundred known errors. This script fails the build only on
# NEW errors:
#
#   Mechanism: run mypy, keep the "... error: ..." lines, and NORMALIZE them
#   by stripping the :LINE[:COL]: positions, leaving
#   "path.py: error: message [code]". The sorted normalized lines are compared
#   against tests/mypy-baseline.txt as a MULTISET (comm): a line occurring
#   more often than in the baseline is a new error and fails the step.
#   Fewer errors than the baseline always passes (fixes are welcome — refresh
#   with --write to keep the baseline honest). Normalization makes the check
#   immune to line-number drift from unrelated edits. Version drift is pinned
#   on both axes: mypy itself (requirements-test.txt pins mypy==2.3.1) and the
#   typeshed render target (python_version in mypy.ini) — without the latter,
#   the same mypy renders messages differently per interpreter (e.g. int()
#   gains SupportsTrunc on 3.11 vs 3.14) and CI legs disagree with a baseline
#   written on another Python.
#
# Scope is the non-test code, matching the audit's baseline (tests excluded).
# --explicit-package-bases is required: plugins/*/routes.py would otherwise
# collide as the module name "routes".
#
# Usage:
#   scripts/check_mypy.sh           # fail on new errors (CI mode)
#   scripts/check_mypy.sh --write   # regenerate tests/mypy-baseline.txt
set -u
export LC_ALL=C   # sort/comm must agree with the committed baseline on any host
cd "$(dirname "$0")/.."

BASELINE=tests/mypy-baseline.txt
SCOPE="runtime web tools plugins scripts"
if [ -x .venv/bin/mypy ]; then
    MYPY=.venv/bin/mypy
else
    MYPY=mypy
fi
# Fail closed: without this guard a missing mypy yields an empty error set and
# the comm diff against the baseline passes (audit #24 D6).
if ! command -v "$MYPY" >/dev/null 2>&1; then
    echo "mypy: '$MYPY' not found — install requirements-test.txt"
    exit 1
fi

raw="$("$MYPY" --explicit-package-bases $SCOPE 2>&1)"
summary="$(printf '%s\n' "$raw" | grep -E '^Found [0-9]+ error' || true)"
# import-not-found/import-untyped are environment noise, not code signals:
# the CI install set (requirements.lock) deliberately lacks the optional
# tool deps (playwright, Pillow, pypdf, mcp, …) and type stubs, so a host
# without them reports errors a full dev venv never sees. Real import
# breakage is covered by the test suite actually importing these modules.
current="$(printf '%s\n' "$raw" | grep ': error:' \
    | grep -vE '\[(import-not-found|import-untyped)\]$' \
    | sed -E 's/^([^:]+):[0-9]+(:[0-9]+)?: (error:.*)$/\1: \3/' | sort || true)"

if [ "${1:-}" = "--write" ]; then
    if [ -n "$current" ]; then
        printf '%s\n' "$current" > "$BASELINE"
    else
        : > "$BASELINE"
    fi
    echo "baseline written: $(grep -c . "$BASELINE") normalized errors ($summary)"
    exit 0
fi

if [ ! -f "$BASELINE" ]; then
    echo "mypy: $BASELINE missing — generate it with scripts/check_mypy.sh --write"
    exit 1
fi

new="$(if [ -n "$current" ]; then printf '%s\n' "$current"; fi | comm -13 "$BASELINE" -)"
if [ -n "$new" ]; then
    echo "mypy: NEW errors not covered by $BASELINE:"
    printf '%s\n' "$new"
    echo "mypy: fix them, or accept them deliberately with scripts/check_mypy.sh --write"
    exit 1
fi

have=0; [ -n "$current" ] && have=$(printf '%s\n' "$current" | grep -c .)
base=$(grep -c . "$BASELINE" || true)
if [ "$have" -lt "$base" ]; then
    echo "mypy: $have normalized errors < baseline $base — consider scripts/check_mypy.sh --write"
fi
echo "mypy: OK — $have normalized errors, all covered by the baseline ($summary)"
