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


def test_version_has_matching_release_record():
    version = runtime.__version__
    path = ROOT / "docs" / "releases" / f"v{version}.md"
    assert path.is_file(), f"missing release record: {path}"
    heading = next((line for line in path.read_text(encoding="utf-8").splitlines()
                    if line.startswith("# ")), "")
    assert re.match(rf"^# (?:JayNet )?v{re.escape(version)}(?:\s|$)", heading), (
        f"release heading {heading!r} does not name v{version}")


def test_prose_version_markers_match():
    """Audit #30 D1: the README/guide/development prose markers drifted two
    releases behind twice — the tag gate (scripts/tag_release.py) reads them
    at HEAD, and this test fails CI the moment they drift again."""
    version = runtime.__version__
    series = version.rsplit(".", 1)[0]
    markers = ((ROOT / "README.md",
                rf"Status: \*\*v{re.escape(version)}\*\*"),
               (ROOT / "LEARNING_GUIDE.md",
                rf"JayNet v{re.escape(series)}\.x"),
               (ROOT / "docs" / "development.md",
                rf"Current: \*\*{re.escape(series)}\.x\*\*"))
    for path, pattern in markers:
        assert re.search(pattern, path.read_text(encoding="utf-8")), (
            f"{path.name} version marker is stale — bump it to {version} "
            "in the release commit (tag_release.py --check enforces this)")
