"""Eval-case → strength mapping: which registered strength tag(s)
(models.strengths in runtime.yaml) a case exercises.

Cases keep their free-form `tags:`; this module is the ONE place that
translates them into the strength vocabulary, so measurement (the strength
matrix in eval_store) and later routing never disagree about what a case
tests. Two ways in, combined:

1. The mapping table below — existing tags carry a strength meaning
   (`security`-tagged cases exercise the security strength, imported
   Terminal-Bench cases (`tb`) exercise coding, …).
2. An explicit per-case escape hatch: a case tag `strength:<tag>` pins the
   case to that strength directly (wins in ADDITION to the table — use it
   for cases whose free-form tags don't cover what they really test).
"""

# strength tag → case tags that exercise it
CASE_STRENGTH_TAGS: dict[str, frozenset[str]] = {
    "coding": frozenset({
        "code", "verify", "delegation", "refactor", "bugfix", "tb",
    }),
    "research": frozenset({
        "web", "freshness", "research", "gaia", "deep-research",
    }),
    "reasoning": frozenset({
        "planning", "trap", "rlm", "long-context", "logic", "j-space",
    }),
    "security": frozenset({
        "security", "injection", "privacy",
    }),
    "vision": frozenset({
        "vision", "image",
    }),
}

_EXPLICIT_PREFIX = "strength:"


def case_strengths(tags: list[str]) -> set[str]:
    """The strength tags a case exercises, from its free-form tag list."""
    tagset = set(tags or [])
    out: set[str] = {t[len(_EXPLICIT_PREFIX):] for t in tagset
                     if t.startswith(_EXPLICIT_PREFIX)}
    for strength, case_tags in CASE_STRENGTH_TAGS.items():
        if tagset & case_tags:
            out.add(strength)
    return out
