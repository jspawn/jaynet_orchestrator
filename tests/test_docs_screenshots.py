"""Docs ↔ screenshots contract (audit #26, bottom-line gate): a doc that
references screenshots/foo.png must not 404 the image — nothing in CI
checked that a referenced PNG exists, and the screenshot sweep's renames
(12 files deleted in one sweep) are exactly how such references break."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_referenced_screenshots_exist():
    docs = list((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]
    refs = set()
    for d in docs:
        refs |= set(re.findall(r"screenshots/[\w.-]+\.png",
                               d.read_text(encoding="utf-8")))
    assert refs, "no screenshot references found — the pattern broke?"
    missing = sorted(r for r in refs if not (ROOT / r).exists())
    assert not missing, f"docs reference screenshots that don't exist: {missing}"
