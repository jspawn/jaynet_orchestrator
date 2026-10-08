"""Unit tests for the baseline-growth gates (2026-10-08 discipline):

- scripts/check_baseline_bump.py: paren_map/growth pure logic — growth
  detection separates "banked fixes" (pure shrink, no marker) from
  "raised ceilings" (marker required).
- scripts/check_prompt_size.py: word counting + budget parse.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bump = _load("check_baseline_bump", "scripts/check_baseline_bump.py")
psize = _load("check_prompt_size", "scripts/check_prompt_size.py")


def test_paren_map_parses_and_skips():
    text = "# comment\n\nruntime/loop.py: run (255)\nweb/server.py: create_app (84)\n"
    assert bump.paren_map(text) == {"runtime/loop.py: run": 255,
                                    "web/server.py: create_app": 84}


def test_growth_paren_flags_added_and_raised_only():
    old = "a.py: f (30)\nb.py: g (40)\n"
    assert bump.growth("paren", old, "a.py: f (30)\n") == []            # shrink
    assert bump.growth("paren", old, "a.py: f (28)\nb.py: g (40)\n") == []  # lowered
    assert bump.growth("paren", old, "a.py: f (31)\nb.py: g (40)\n") == \
        ["raised: a.py: f (30 → 31)"]
    assert bump.growth("paren", old, old + "c.py: h (26)\n") == \
        ["added: c.py: h (26)"]


def test_growth_lines_multiset():
    old = "e.py: error: one  [x]\ne.py: error: two  [y]\n"
    assert bump.growth("lines", old, "e.py: error: one  [x]\n") == []   # fix banked
    assert bump.growth("lines", old, old) == []
    grew = bump.growth("lines", old, old + "f.py: error: three  [z]\n")
    assert grew == ["added: f.py: error: three  [z]"]
    # duplicate occurrences count multiset-style
    dup = bump.growth("lines", "a\n", "a\na\n")
    assert dup == ["added: a"]


def test_prompt_budget_file_matches_gate_prompt():
    """The committed budget parses and the watched file exists."""
    budget = psize.load_budget()
    assert "prompts/orchestrator-gate.md" in budget
    assert budget["prompts/orchestrator-gate.md"] > 0
    for p in psize.WATCHED:
        assert (ROOT / p).exists(), f"watched prompt missing: {p}"


def test_prompt_word_count_not_over_budget():
    """Same assertion the CI gate makes (local mirror; --write to bank a diet)."""
    budget = psize.load_budget()
    for p in psize.WATCHED:
        assert psize.word_count(ROOT / p) <= budget[p], (
            f"{p} exceeds its word budget — diet it or bump deliberately")
