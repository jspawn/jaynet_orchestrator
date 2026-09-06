"""Readiness audit QA-9: nothing tied the version string to the release
record — the tag convention already broke once (v1.7.0 tagged an ordinary
feature commit) and master sat 5 commits past the newest tag with the
version unchanged. The one number the operator is told to check after an
update must identify the code actually running."""
import re
from pathlib import Path

import runtime

ROOT = Path(__file__).resolve().parent.parent


def test_version_matches_changelog_head():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## (\d+\.\d+\.\d+)", changelog, flags=re.M)
    assert m, "CHANGELOG.md has no '## X.Y.Z' entry"
    assert runtime.__version__ == m.group(1), (
        f"runtime.__version__ ({runtime.__version__}) != CHANGELOG top entry "
        f"({m.group(1)}) — bump both in the release commit, then tag "
        f"v{runtime.__version__}")
