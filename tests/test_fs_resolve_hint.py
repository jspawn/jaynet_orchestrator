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


def test_fictional_absolute_root_rebases_onto_workspace():
    """Container-style absolute paths (/app/...) copied from a task statement
    rebase onto the work_root — the workspace IS /app from the model's view."""
    root = _root()
    p = resolve_in_roots([root], "/app/3 Custodian activities/FMT_CA_2.md")
    assert p == root / "3 Custodian activities" / "FMT_CA_2.md"
    # The bare fictional root maps to the workspace itself (fs.list /app).
    assert resolve_in_roots([root], "/app") == root


def test_real_absolute_path_still_refused():
    """A first component that EXISTS on the host (/etc, /tmp, /srv) is a real
    escape attempt, not a container artifact — hard PermissionError stands."""
    root = _root()
    with pytest.raises(PermissionError):
        resolve_in_roots([root], "/etc/passwd")


def test_skill_file_read_refusal_is_actionable(ctx, tmp_path):
    """fs.read of a bundled skill script (outside the workspace) must say
    RUN it via code.check — the bare refusal made brains hand-write their
    own converters instead (docx run, 2026-10-13)."""
    from runtime import paths
    from tools.fs.ops import _resolve
    c = ctx(work_root=tmp_path, tmp_root=tmp_path)
    script = paths.SKILLS_DIR / "docx" / "write_docx.py"
    with pytest.raises(PermissionError) as ei:
        _resolve(c, str(script))
    msg = str(ei.value)
    assert "skill file" in msg and "code.check" in msg and "RUN it" in msg


def test_skill_md_read_is_allowed(ctx, tmp_path):
    """skill.load hands the model absolute paths of prose module docs to
    READ (skills/j-space/SKILL.md directs the agent at modules/*.md) —
    refusing them burned turns on doomed retries and stalled the run
    (j-space-floor eval, 2026-10-22). .md under the skills dirs resolves."""
    from runtime import paths
    from tools.fs.ops import _resolve
    c = ctx(work_root=tmp_path, tmp_root=tmp_path)
    doc = paths.SKILLS_DIR / "j-space" / "modules" / "capacity.md"
    assert _resolve(c, str(doc)) == doc.resolve()
    assert _resolve(c, str(paths.SKILLS_DIR / "docx" / "SKILL.md")).name == "SKILL.md"


def test_skill_md_read_missing_file_is_not_found(ctx, tmp_path):
    from runtime import paths
    from tools.fs.ops import _resolve
    c = ctx(work_root=tmp_path, tmp_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        _resolve(c, str(paths.SKILLS_DIR / "docx" / "no-such-doc.md"))


def test_skill_md_edit_still_refused(ctx, tmp_path):
    """The .md allowance is read-only: fs.edit on a skill doc must still get
    the read-only refusal, not the read-through path."""
    from runtime import paths
    from tools.fs.ops import _resolve
    c = ctx(work_root=tmp_path, tmp_root=tmp_path)
    doc = paths.SKILLS_DIR / "j-space" / "modules" / "capacity.md"
    with pytest.raises(PermissionError) as ei:
        _resolve(c, str(doc), write=True)
    assert "read-only" in str(ei.value)


def test_skill_file_write_refusal_points_at_studio(ctx, tmp_path):
    from runtime import paths
    from tools.fs.ops import _resolve
    c = ctx(work_root=tmp_path, tmp_root=tmp_path)
    script = paths.SKILLS_DIR / "docx" / "write_docx.py"
    with pytest.raises(PermissionError) as ei:
        _resolve(c, str(script), must_exist=False)   # the write path
    assert "read-only" in str(ei.value)


def test_non_skill_outside_path_keeps_generic_refusal(ctx, tmp_path):
    from tools.fs.ops import _resolve
    c = ctx(work_root=tmp_path, tmp_root=tmp_path)
    with pytest.raises(PermissionError) as ei:
        _resolve(c, "/etc/passwd")
    assert "outside your workspace" in str(ei.value)
