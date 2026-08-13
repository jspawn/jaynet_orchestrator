"""resolve_in_roots: not-found errors are actionable and suggest close names.

Covers the real incident: a path with a space-vs-underscore (or trailing-space)
mismatch resolves to a non-existent path; the error should point at the actual
name instead of a bare path.
"""
import tempfile
from pathlib import Path

import pytest

from runtime.tool_base import resolve_in_roots


def _root():
    d = tempfile.mkdtemp()
    root = Path(d)
    # real folder uses a SPACE after the number, like the FMO tree on disk
    (root / "3 Custodian activities").mkdir()
    (root / "3 Custodian activities" / "FMT_CA_2.md").write_text("x")
    return root


def test_existing_path_resolves():
    root = _root()
    p = resolve_in_roots([root], "3 Custodian activities/FMT_CA_2.md")
    assert p.name == "FMT_CA_2.md"


def test_space_vs_underscore_suggests_real_dir():
    root = _root()
    with pytest.raises(FileNotFoundError) as ei:
        resolve_in_roots([root], "3_Custodian activities/FMT_CA_2.md")  # underscore
    msg = str(ei.value)
    assert "similar name exists" in msg and "3 Custodian activities" in msg


def test_missing_file_with_no_close_match_names_the_dir():
    root = _root()
    with pytest.raises(FileNotFoundError) as ei:
        resolve_in_roots([root], "3 Custodian activities/zzz-unrelated.dat")
    msg = str(ei.value)
    assert "zzz-unrelated.dat" in msg and "does not exist in" in msg


def test_close_typo_is_suggested():
    root = _root()
    with pytest.raises(FileNotFoundError) as ei:
        resolve_in_roots([root], "3 Custodian activities/FMT_CA_9.md")  # near FMT_CA_2.md
    assert "similar name exists" in str(ei.value)


def test_trailing_space_suggests_match():
    root = _root()
    with pytest.raises(FileNotFoundError) as ei:
        resolve_in_roots([root], "3 Custodian activities /FMT_CA_2.md")  # trailing space
    assert "similar name exists" in str(ei.value)


def test_outside_workspace_still_permission_error():
    root = _root()
    with pytest.raises(PermissionError):
        resolve_in_roots([root], "/etc/passwd")
