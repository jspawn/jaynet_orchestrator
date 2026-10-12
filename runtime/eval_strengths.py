"""Eval-case → strength mapping: which registered strength tag(s)
(models.strengths in runtime.yaml) a case exercises.

Cases keep their free-form `tags:`; this module is the ONE place that
translates them into the strength vocabulary, so measurement (the strength
matrix in eval_store) and later routing never disagree about what a case
tests. Three ways in, combined:

1. The alias table below — existing tags carry a strength meaning
   (`tb` → coding, `gaia` → research, …) for benchmark-suite tags that
   aren't strength names themselves.
2. An explicit per-case escape hatch: a case tag `strength:<tag>` pins the
   case to that strength directly (wins in ADDITION to the table — use it
   for cases whose free-form tags don't cover what they really test).
3. A direct registry match: a case tag that IS a registered strength tag
   counts under it — the registry is admin-editable (Admin → Models →
   Strengths), so a new tag like `bugtrace` gets measured without a code
   change.
"""

# strength tag → case tags that exercise it (aliases for tags that aren't
# strength names themselves; direct registry matches need no entry)
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


def case_strengths(tags: list[str], registry: dict | None = None) -> set[str]:
    """The strength tags a case exercises, from its free-form tag list.
    `registry` = models.strengths (admin-editable): a case tag that IS a
    registered tag counts under it directly."""
    tagset = set(tags or [])
    out: set[str] = {t[len(_EXPLICIT_PREFIX):] for t in tagset
                     if t.startswith(_EXPLICIT_PREFIX)}
    if registry:
        out |= tagset & set(registry)
    for strength, case_tags in CASE_STRENGTH_TAGS.items():
        if tagset & case_tags:
            out.add(strength)
    return out
