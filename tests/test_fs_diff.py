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


# --------------------------------------------- fail-safe matching (audit
# --------------------------------------------- 2026-10-05 finding 2)

def test_edit_ws_normalization_never_migrates_indentation(ctx, project):
    """Audit repro 2a: the old every-whitespace-run-is-one-space matcher
    matched a span whose lines sat at DIFFERENT indentation and spliced
    new_str back there — a statement silently migrated out of its block
    with status ok. Line structure (newlines AND leading indentation) must
    now match exactly, so this edit fails loudly and the file is
    untouched."""
    c = ctx()
    run(FsWrite().execute({"path": "m.py", "content": (
        "def f(items):\n"
        "    total = 0\n"
        "    for i in items:\n"
        "        total += i\n"
        "    count += 1\n"          # 4-space indent — NOT the 8 the old_str claims
        "    return total\n")}, c))
    res = run(FsEdit().execute({"path": "m.py",
                                "old_str": "        total += i\n        count += 1",
                                "new_str": "        total += i"}, c))
    assert res.status == "error" and "old_str not found" in res.error
    assert (project / "m.py").read_text().endswith(
        "    count += 1\n    return total\n")


def test_edit_new_str_dict_keys_survive(ctx, project):
    """Audit repro 2b: old_str carried copied line-number prefixes and the
    tool stripped `^\\s*\\d+[:\\t]` from new_str too — new dict entries
    `1: 'one'` lost their keys. new_str is de-prefixed only when EVERY
    line is prefixed now."""
    c = ctx()
    run(FsWrite().execute({"path": "d.py", "content": "names = {}\n"}, c))
    res = run(FsEdit().execute({
        "path": "d.py",
        "old_str": "     1\tnames = {}",
        "new_str": "names = {\n    1: 'one',\n    2: 'two',\n}"}, c))
    assert res.status == "ok"
    assert "new_str prefixes stripped" not in res.result.get("note", "")
    assert (project / "d.py").read_text() == \
        "names = {\n    1: 'one',\n    2: 'two',\n}\n"


def test_edit_ws_normalization_multiline_same_structure(ctx, project):
    """The happy path the normalization exists for: identical line
    structure and indentation, only intra-line spacing drifted."""
    c = ctx()
    run(FsWrite().execute({"path": "w2.py", "content": (
        "def f():\n"
        "    x = combine(1,  2)\n"
        "    return  x\n")}, c))
    res = run(FsEdit().execute({
        "path": "w2.py",
        "old_str": "    x = combine(1, 2)\n    return x",
        "new_str": "    x = combine(1, 3)\n    return x"}, c))
    assert res.status == "ok"
    assert res.result["note"] == "matched after whitespace normalization"
    assert (project / "w2.py").read_text() == \
        "def f():\n    x = combine(1, 3)\n    return x\n"


def test_edit_result_carries_the_diff(ctx, project):
    """2c: the unified diff is IN the tool result so the model sees what
    actually changed (covered above; pinned here against the fuzzy paths
    too)."""
    c = ctx()
    run(FsWrite().execute({"path": "g.txt", "content": "keep  this\n"}, c))
    res = run(FsEdit().execute({"path": "g.txt", "old_str": "keep this",
                                "new_str": "KEEP"}, c))
    assert res.status == "ok"
    assert "-keep  this" in res.result["diff"] and "+KEEP" in res.result["diff"]
