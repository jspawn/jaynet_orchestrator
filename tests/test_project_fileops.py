"""Project/scratch file ops backing the modal file manager: mkdir + move_path."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, "web")
import projects as PJ


def _root():
    r = Path(tempfile.mkdtemp())
    (r / "a").mkdir()
    (r / "a" / "f.txt").write_text("x")
    return r


def test_mkdir_nested_and_guards():
    r = _root()
    assert PJ.mkdir(r, "new folder/sub")["type"] == "dir"
    assert (r / "new folder" / "sub").is_dir()
    assert PJ.mkdir(r, "") is None            # empty
    assert PJ.mkdir(r, "../escape") is None   # traversal


def test_rename_file_preserves_spaces():
    r = _root()
    out = PJ.move_path(r, "a/f.txt", "a/renamed file.txt")
    assert out["to"] == "a/renamed file.txt"
    assert (r / "a" / "renamed file.txt").exists() and not (r / "a" / "f.txt").exists()


def test_rename_folder():
    r = _root()
    assert PJ.move_path(r, "a", "b")
    assert (r / "b").is_dir() and not (r / "a").exists()


def test_move_guards():
    r = _root()
    (r / "x.txt").write_text("1"); (r / "y.txt").write_text("2")
    assert PJ.move_path(r, "x.txt", "y.txt") is None      # dest exists (no overwrite)
    assert PJ.move_path(r, "x.txt", "../out.txt") is None  # escape root
    assert PJ.move_path(r, "a", "a/inner") is None         # into own subtree
    assert PJ.move_path(r, "nope", "z") is None            # missing source
    assert PJ.move_path(r, "x.txt", "") is None            # empty dest
