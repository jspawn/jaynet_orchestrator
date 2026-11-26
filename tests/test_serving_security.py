"""Serving registry path confinement and bounded log-tail reads."""
import pytest

from runtime.serving import _server_dir, tail


def test_server_dir_rejects_traversal_absolute_and_symlink_escape(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    assert _server_dir(root, "brain-1") == (root / "brain-1").resolve()
    for bad in ("../escape", "..", "a/b", "/tmp/escape"):
        with pytest.raises(ValueError):
            _server_dir(root, bad)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        _server_dir(root, "linked")


def test_tail_reads_only_newest_complete_lines(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("x" * 100000 + "\nlast-one\nlast-two\n")
    assert tail(log, 2) == "last-one\nlast-two"
    small = tmp_path / "small.log"
    small.write_text("first\nsecond")
    assert tail(small, 2) == "first\nsecond"
