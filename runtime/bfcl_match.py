"""BFCL-style function-call matcher (AST grading, Python-only reimplementation).

Grades a run's actual stub-tool calls against BFCL ground truth. The ground
truth shape mirrors the official `possible_answer` files so the benchlab
importer can pass rows through unchanged:

  [{"func_name": {"param": [acceptable, values], "opt": ["", "default"]}}, ...]

Rules (from ShishirPatil/gorilla ast_checker.py, Python path):
  - call COUNT must match the ground truth exactly (parallel included);
    matching is order-insensitive via greedy elimination
  - a param whose acceptable list contains "" is OPTIONAL (may be omitted);
    every other ground-truth param must be present
  - every actual param must appear in the ground truth (unexpected_param)
  - values compare type-tolerantly: int promotes to float, strings compare
    standardized (case/punctuation-insensitive), lists elementwise in order,
    dicts key-by-key with the same optional-"" rule
  - an acceptable value of "" on a PRESENT param also matches (it means
    "default/empty is acceptable")

Deliberately not ported: Java/JS tree-sitter converters, multi-turn state
grading, and the schema's own required list (the ground truth's "" marker
carries the same information).
"""

from __future__ import annotations

import re
from typing import Any

_STRIP_CHARS = " ,./-_*^"


def standardize_string(value: Any) -> str:
    """Case/punctuation-insensitive string comparison key (BFCL
    standardize_string: lowercase; strip spaces and ,./-_*^; ' -> ")."""
    s = str(value).lower().replace("'", '"')
    return s.translate(str.maketrans("", "", _STRIP_CHARS))


_NAME_SANITIZE = re.compile(r"[^A-Za-z0-9_.]")


def sanitize_name(name: str) -> str:
    """BFCL function names carry dots and the odd dash; JayNet tool names are
    namespace.verb with [A-Za-z0-9_.] — map everything else to '_'."""
    return _NAME_SANITIZE.sub("_", str(name))


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _value_matches(actual: Any, acceptable: Any) -> bool:
    """One actual value against ONE acceptable value (see module rules).
    The optional-marker "" never reaches here — callers strip it; omission
    licensing lives in the param-presence checks, not value matching."""
    if isinstance(acceptable, str):
        if isinstance(actual, str):
            return standardize_string(actual) == standardize_string(acceptable)
        if _is_num(actual) or isinstance(actual, bool):
            # "variable" rule: schema type non-string, gt strings → compare
            # as strings (bools render Python-style in BFCL gt)
            return standardize_string(actual) == standardize_string(acceptable)
        return False
    if _is_num(acceptable):
        return _is_num(actual) and float(actual) == float(acceptable)
    if isinstance(acceptable, bool):
        return actual is acceptable
    if isinstance(acceptable, (list, tuple)):
        if not isinstance(actual, (list, tuple)) \
                or len(actual) != len(acceptable):
            return False
        return all(_value_matches(a, e) for a, e in zip(actual, acceptable))
    if isinstance(acceptable, dict):
        if not isinstance(actual, dict):
            return False
        for key, opts in acceptable.items():
            opts_l = [o for o in (opts if isinstance(opts, list) else [opts])
                      if o != ""]
            full_l = opts if isinstance(opts, list) else [opts]
            if key not in actual:
                if len(opts_l) == len(full_l):     # no "" marker = required
                    return False
                continue
            if opts_l and not any(_value_matches(actual[key], o)
                                  for o in opts_l):
                return False
        return True
    return actual == acceptable


def _call_matches(actual_name: str, actual_args: dict,
                  expected: dict[str, dict[str, list]]) -> str | None:
    """One actual call against one ground-truth call; returns a failure
    string or None on match. `expected` is the single-key {name: params}."""
    exp_name, exp_params = next(iter(expected.items()))
    if actual_name != exp_name:
        return f"called '{actual_name}', expected '{exp_name}'"
    actual_args = actual_args or {}
    for param in actual_args:
        if param not in exp_params:
            return (f"unexpected param '{param}' for '{exp_name}' "
                    f"(gt has: {sorted(exp_params)})")
    for param, acceptable in exp_params.items():
        raw = acceptable if isinstance(acceptable, list) else [acceptable]
        opts = [o for o in raw if o != ""]
        if param not in actual_args:
            if len(opts) < len(raw):             # "" marker = may be omitted
                continue
            return f"missing required param '{param}' for '{exp_name}'"
        if opts and not any(_value_matches(actual_args[param], o)
                            for o in opts):
            return (f"param '{param}'={actual_args[param]!r} matches none of "
                    f"{opts!r} for '{exp_name}'")
    return None


def match_calls(expected: list[dict],
                actual: list[tuple[str, dict]]) -> list[str]:
    """Grade actual calls [(tool_name, args), ...] against BFCL ground truth.
    Returns failure strings (empty = pass). Count must match exactly;
    >1 expected call matches order-insensitively (greedy elimination)."""
    if not expected:
        if actual:
            names = ", ".join(n for n, _ in actual)
            return [f"irrelevance: no function call expected, "
                    f"but {len(actual)} fired ({names})"]
        return []
    failures: list[str] = []
    if len(actual) != len(expected):
        failures.append(
            f"expected exactly {len(expected)} call(s), got {len(actual)}"
            + (f" ({', '.join(n for n, _ in actual)})" if actual else ""))
        return failures
    remaining = list(actual)
    for exp in expected:
        exp_name = next(iter(exp))
        best_idx = None
        best_err = None
        for i, (name, args) in enumerate(remaining):
            if name != exp_name:
                continue
            err = _call_matches(name, args, exp)
            if err is None:
                best_idx, best_err = i, None
                break
            if best_err is None:
                best_idx, best_err = i, err
        if best_idx is None:
            failures.append(f"expected call to '{exp_name}' never fired "
                            f"(got: {', '.join(n for n, _ in remaining)})")
            continue
        remaining.pop(best_idx)
        if best_err:
            failures.append(best_err)
    return failures
