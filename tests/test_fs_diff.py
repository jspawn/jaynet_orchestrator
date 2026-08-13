"""fs.edit / fs.write change reporting: action status + capped unified diff.

The chat UI renders `action` as a brief badge and `diff` inline; the fields are
additive to the result dict (path/bytes/replaced stay)."""
from __future__ import annotations

from conftest import run

from tools.fs.ops import FsEdit, FsWrite


def test_write_new_file_reports_created(ctx):
    res = run(FsWrite().execute({"path": "new.txt", "content": "a\nb\n"}, ctx()))
    assert res.status == "ok"
    r = res.result
    assert r["action"] == "created"
    assert r["lines"] == 2
    assert r["bytes"] == 4
    assert "diff" not in r                       # writes never carry a diff


def test_write_existing_reports_overwritten(ctx):
    c = ctx()
    run(FsWrite().execute({"path": "f.txt", "content": "v1"}, c))
    res = run(FsWrite().execute({"path": "f.txt", "content": "v2"}, c))
    assert res.result["action"] == "overwritten"


def test_write_append_reports_appended(ctx):
    c = ctx()
    run(FsWrite().execute({"path": "f.txt", "content": "v1"}, c))
    res = run(FsWrite().execute({"path": "f.txt", "content": "v2", "mode": "append"}, c))
    assert res.result["action"] == "appended"


def test_edit_returns_short_diff(ctx):
    c = ctx()
    run(FsWrite().execute({"path": "f.txt", "content": "one\ntwo\nthree\n"}, c))
    res = run(FsEdit().execute({"path": "f.txt", "old_str": "two", "new_str": "TWO"}, c))
    assert res.status == "ok"
    r = res.result
    assert r["action"] == "edited" and r["replaced"] == 1
    assert r["added"] == 1 and r["removed"] == 1
    assert "-two" in r["diff"] and "+TWO" in r["diff"]
    assert r["diff"].startswith("@@")
    assert r["diff_truncated"] is False


def test_edit_diff_is_capped(ctx):
    c = ctx()
    old = "\n".join(f"line {i}" for i in range(500))
    new = "\n".join(f"LINE {i}" for i in range(500))
    run(FsWrite().execute({"path": "big.txt", "content": old}, c))
    res = run(FsEdit().execute({"path": "big.txt", "old_str": old, "new_str": new}, c))
    r = res.result
    assert r["diff_truncated"] is True
    assert "diff truncated" in r["diff"]
    assert len(r["diff"].splitlines()) <= 41     # 40 cap + truncation note
    assert r["added"] == 500 and r["removed"] == 500


def test_edit_error_paths_unchanged(ctx):
    c = ctx()
    run(FsWrite().execute({"path": "f.txt", "content": "x x"}, c))
    res = run(FsEdit().execute({"path": "f.txt", "old_str": "nope", "new_str": "y"}, c))
    assert res.status == "error" and "not found" in res.error
    res = run(FsEdit().execute({"path": "f.txt", "old_str": "x", "new_str": "y"}, c))
    assert res.status == "error" and "2 times" in res.error
