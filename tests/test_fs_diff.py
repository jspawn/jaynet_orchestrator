"""fs.edit / fs.write change reporting: action status + capped unified diff.

The chat UI renders `action` as a brief badge and `diff` inline; the fields are
additive to the result dict (path/bytes/replaced stay).

Also covers fs.read's plain-text model rendering (JSON escaping is what small
models copied into edits) and fs.edit's forgiving matching fallbacks
(exact → line-prefix-stripped → whitespace-normalized → closest-snippet)."""
from __future__ import annotations

import json

from conftest import run

from tools.fs.ops import FsEdit, FsRead, FsWrite


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


# ------------------------------------------------------- fs.read plain text
def test_read_model_message_is_plain_text(ctx):
    c = ctx()
    run(FsWrite().execute(
        {"path": "shop.txt", "content": "apples\nbread\tsoda\n\"milk\"\n"}, c))
    res = run(FsRead().execute({"path": "shop.txt"}, c))
    assert res.status == "ok"
    # The result dict keeps its exact shape for programmatic consumers.
    r = res.result
    assert r["lines"] == "1-3 of 3" and r["truncated_bytes"] is False
    assert r["content"].splitlines()[1] == "     2\tbread\tsoda"
    msg = res.to_model_message()
    # Header + raw content: line-number prefixes kept, JSON escaping gone.
    assert msg.startswith("# shop.txt (lines 1-3 of 3)\n")
    assert "     2\tbread\tsoda" in msg          # raw tab survives
    assert '"milk"' in msg                       # raw quotes, not \"
    assert "\\t" not in msg and "\\n" not in msg


def test_read_plain_text_marks_truncation(ctx, project):
    (project / "big.txt").write_text("x" * 5000)
    res = run(FsRead().execute({"path": "big.txt", "max_bytes": 100}, ctx()))
    assert res.result["truncated_bytes"] is True
    assert res.to_model_message().splitlines()[0] == \
        "# big.txt (lines 1-1 of 1) [truncated by max_bytes]"


def test_read_error_still_json(ctx):
    res = run(FsRead().execute({"path": "nope.txt"}, ctx()))
    assert res.status == "error"
    assert json.loads(res.to_model_message())["status"] == "error"


# --------------------------------------------- fs.edit forgiving matching
def test_edit_strips_copied_line_prefixes(ctx, project):
    """The live-trace artifact: the model copies fs.read's numbered rendering
    (`     3\tbread`) straight into old_str. It must just work."""
    c = ctx()
    run(FsWrite().execute(
        {"path": "shopping.txt", "content": "apples\nbread\nmilk\n"}, c))
    res = run(FsEdit().execute({"path": "shopping.txt",
                                "old_str": "     2\tbread",
                                "new_str": "rye bread"}, c))
    assert res.status == "ok"
    assert "line-number prefixes" in res.result["note"]
    assert (project / "shopping.txt").read_text() == "apples\nrye bread\nmilk\n"


def test_edit_strips_prefixes_multiline_and_new_str(ctx, project):
    c = ctx()
    run(FsWrite().execute(
        {"path": "s.txt", "content": "alpha\nbeta\ngamma\n"}, c))
    res = run(FsEdit().execute({"path": "s.txt",
                                "old_str": "     1\talpha\n     2\tbeta",
                                "new_str": "     1\tALPHA\n     2\tBETA"}, c))
    assert res.status == "ok"
    assert "new_str prefixes stripped too" in res.result["note"]
    # new_str got the same de-prefixing — the file is NOT polluted with numbers.
    assert (project / "s.txt").read_text() == "ALPHA\nBETA\ngamma\n"


def test_edit_legit_digits_tab_content_not_corrupted(ctx, project):
    """A file that genuinely contains `3\tbread` (TSV-ish): exact match is
    tried FIRST, so prefix-stripping never fires on legitimate content."""
    c = ctx()
    run(FsWrite().execute(
        {"path": "data.tsv", "content": "3\tbread\n4\tmilk\n"}, c))
    res = run(FsEdit().execute({"path": "data.tsv",
                                "old_str": "3\tbread", "new_str": "3\trolls"}, c))
    assert res.status == "ok"
    assert "note" not in res.result              # exact path: no fallback fired
    assert (project / "data.tsv").read_text() == "3\trolls\n4\tmilk\n"
    # Even the read-style spaced form, when literally present, matches exactly.
    run(FsWrite().execute(
        {"path": "lit.txt", "content": "     3\tbread\nx\n"}, c))
    res = run(FsEdit().execute({"path": "lit.txt",
                                "old_str": "     3\tbread", "new_str": "y"}, c))
    assert res.status == "ok" and "note" not in res.result
    assert (project / "lit.txt").read_text() == "y\nx\n"


def test_edit_whitespace_drift_matches_real_text(ctx, project):
    """Model sends single-spaced where the file has double: matches via
    whitespace normalization, spliced into the REAL file bytes."""
    c = ctx()
    run(FsWrite().execute(
        {"path": "w.txt", "content": "keep  this\nand  that\n"}, c))
    res = run(FsEdit().execute({"path": "w.txt",
                                "old_str": "keep this", "new_str": "KEEP"}, c))
    assert res.status == "ok"
    assert res.result["note"] == "matched after whitespace normalization"
    # The double space outside the replaced span is untouched.
    assert (project / "w.txt").read_text() == "KEEP\nand  that\n"


def test_edit_miss_returns_closest_snippet(ctx, project):
    c = ctx()
    run(FsWrite().execute(
        {"path": "m.txt",
         "content": "def total(items):\n    return sum(items)\n\ndone = True\n"}, c))
    res = run(FsEdit().execute({"path": "m.txt",
                                "old_str": "def total(items):\n    retur sum(items)",
                                "new_str": "x"}, c))
    assert res.status == "error"
    assert "old_str not found" in res.error
    assert "closest match (lines 1-2)" in res.error
    assert "     1\tdef total(items):" in res.error
    assert "     2\t    return sum(items)" in res.error


def test_edit_ambiguous_lists_line_numbers(ctx, project):
    c = ctx()
    run(FsWrite().execute(
        {"path": "a.txt", "content": "dup\nmid\ndup\n"}, c))
    res = run(FsEdit().execute({"path": "a.txt", "old_str": "dup", "new_str": "x"}, c))
    assert res.status == "error"
    assert "2 times" in res.error and "(lines 1, 3)" in res.error
