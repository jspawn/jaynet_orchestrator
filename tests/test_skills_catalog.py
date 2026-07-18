"""Skill catalog completeness: every discovered skill must carry a non-empty
description. The system-prompt catalog (runtime.skills.render_catalog) renders
`- **name** — description`, so a SKILL.md without frontmatter shows up blank
and the model can't tell when to load it."""
from pathlib import Path

import runtime
from runtime.skills import discover_skills, render_catalog

ROOT = Path(runtime.__file__).resolve().parent.parent


def test_every_discovered_skill_has_a_description():
    skills = discover_skills(ROOT / "skills")
    assert skills, "no skills discovered — wrong root?"
    missing = [name for name, s in skills.items() if not s["description"]]
    assert missing == []


def test_rendered_catalog_has_no_blank_descriptions():
    catalog = render_catalog(discover_skills(ROOT / "skills"))
    entries = [l for l in catalog.splitlines() if l.startswith("- **")]
    assert entries, "catalog rendered no skill entries"
    for line in entries:
        assert not line.rstrip().endswith("—"), line
