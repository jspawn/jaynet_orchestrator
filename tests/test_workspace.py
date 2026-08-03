"""Workspace boundary: fs.* is confined to ctx.work_root (+ ephemeral tmp_root),
with NO shared global root. This is the structural isolation the work_root seam
buys us — a run rooted in one project/chat cannot read or write another's files,
and with no workspace at all nothing is writable.
"""
from __future__ import annotations

from pathlib import Path

from conftest import run
from runtime.tool_base import ToolContext
from tools.fs.ops import FsRead, FsWrite

# A config that deliberately sets NO fs.allowed_roots, so the ONLY thing that can
# grant access is ctx.work_root / ctx.tmp_root.
_BARE = {"tools": {"fs": {}}, "trace": {}}


def _ctx(**over):
    return ToolContext(request_id="t", config=_BARE, budget=None, **over)


def test_write_inside_work_root(tmp_path):
    root = tmp_path / "ws"; root.mkdir()
    r = run(FsWrite().execute({"path": str(root / "a.txt"), "content": "hi"}, _ctx(work_root=str(root))))
    assert r.status == "ok"
    assert (root / "a.txt").read_text() == "hi"


def test_write_outside_work_root_refused(tmp_path):
    root = tmp_path / "ws"; root.mkdir()
    outside = tmp_path / "elsewhere"; outside.mkdir()
    r = run(FsWrite().execute({"path": str(outside / "x.txt"), "content": "no"},
                              _ctx(work_root=str(root))))
    assert r.status == "error" and "workspace" in r.error
    assert not (outside / "x.txt").exists()


def test_tmp_root_is_writable(tmp_path):
    root = tmp_path / "ws"; root.mkdir()
    tmp = tmp_path / "tmp"; tmp.mkdir()
    r = run(FsWrite().execute({"path": str(tmp / "scratch.txt"), "content": "tmp"},
                              _ctx(work_root=str(root), tmp_root=str(tmp))))
    assert r.status == "ok" and (tmp / "scratch.txt").read_text() == "tmp"


def test_two_work_roots_are_isolated(tmp_path):
    a = tmp_path / "A"; a.mkdir()
    b = tmp_path / "B"; b.mkdir()
    # Owner A writes a secret in its own root.
    run(FsWrite().execute({"path": str(a / "secret.txt"), "content": "topsecret"},
                          _ctx(work_root=str(a))))
    # A run rooted in B cannot read it.
    r = run(FsRead().execute({"path": str(a / "secret.txt")}, _ctx(work_root=str(b))))
    assert r.status == "error" and "workspace" in r.error


def test_no_workspace_refuses_everything(tmp_path):
    # No work_root, no tmp_root, no configured allowed_roots -> nothing writable.
    r = run(FsWrite().execute({"path": str(tmp_path / "y.txt"), "content": "z"}, _ctx()))
    assert r.status == "error"
    assert not (tmp_path / "y.txt").exists()


def test_sweep_scratch_reclaims_stale_only(tmp_path):
    import os, time
    from runtime.outputs import sweep_scratch
    scratch = tmp_path / "chat-scratch"
    # owner 'u': one fresh chat, one stale chat
    fresh = scratch / "u" / "convFRESH" / "files"; fresh.mkdir(parents=True)
    stale = scratch / "u" / "convSTALE" / "files"; stale.mkdir(parents=True)
    (fresh / "now.txt").write_text("recent")
    old = stale / "old.txt"; old.write_text("ancient")
    # age the stale chat's tree well past a 1h TTL
    long_ago = time.time() - 5 * 3600
    for p in (old, stale, stale.parent, stale.parent.parent):
        os.utime(p, (long_ago, long_ago))
    removed = sweep_scratch(scratch, ttl_hours=1)
    assert removed == 1
    assert not (scratch / "u" / "convSTALE").exists()   # reclaimed
    assert (scratch / "u" / "convFRESH").exists()         # spared (active)


def test_sweep_scratch_missing_root_is_noop(tmp_path):
    from runtime.outputs import sweep_scratch
    assert sweep_scratch(tmp_path / "does-not-exist", ttl_hours=1) == 0


def test_extra_root_is_writable(tmp_path):
    # A caller-granted extra root (e.g. a /llmwiki run's wiki dir) is in bounds.
    root = tmp_path / "ws"; root.mkdir()
    wiki = tmp_path / "wiki"; wiki.mkdir()
    r = run(FsWrite().execute({"path": str(wiki / "index.md"), "content": "# i"},
                              _ctx(work_root=str(root), extra_roots=[str(wiki)])))
    assert r.status == "ok" and (wiki / "index.md").read_text() == "# i"


def test_extra_root_not_writable_without_grant(tmp_path):
    root = tmp_path / "ws"; root.mkdir()
    wiki = tmp_path / "wiki"; wiki.mkdir()
    r = run(FsWrite().execute({"path": str(wiki / "index.md"), "content": "# i"},
                              _ctx(work_root=str(root))))
    assert r.status == "error" and "workspace" in r.error
    assert not (wiki / "index.md").exists()


def test_relative_path_still_anchors_to_work_root_with_extra_roots(tmp_path):
    # roots[0] stays the anchor for relative paths even when extra roots exist.
    root = tmp_path / "ws"; root.mkdir()
    wiki = tmp_path / "wiki"; wiki.mkdir()
    r = run(FsWrite().execute({"path": "note.txt", "content": "x"},
                              _ctx(work_root=str(root), extra_roots=[str(wiki)])))
    assert r.status == "ok" and (root / "note.txt").exists()
    assert not (wiki / "note.txt").exists()


def test_relative_path_lands_in_work_root(tmp_path):
    # The reported regression: a bare relative name must resolve INTO the
    # workspace, not against the process CWD — so no probing is needed.
    root = tmp_path / "ws"; root.mkdir()
    r = run(FsWrite().execute({"path": "empty_file.txt", "content": ""}, _ctx(work_root=str(root))))
    assert r.status == "ok"
    assert (root / "empty_file.txt").exists()
    assert r.result["path"] == str(root / "empty_file.txt")


def test_relative_subdir_path_ok(tmp_path):
    root = tmp_path / "ws"; root.mkdir()
    r = run(FsWrite().execute({"path": "a/b/c.txt", "content": "x"}, _ctx(work_root=str(root))))
    assert r.status == "ok" and (root / "a" / "b" / "c.txt").read_text() == "x"


def test_relative_dotdot_cannot_escape(tmp_path):
    root = tmp_path / "ws"; root.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    r = run(FsWrite().execute({"path": "../secret.txt", "content": "hacked"},
                              _ctx(work_root=str(root))))
    assert r.status == "error" and "workspace" in r.error
    assert (tmp_path / "secret.txt").read_text() == "nope"   # untouched


def test_code_tools_relative_path_anchors_to_work_root(tmp_path):
    # code.tree / code.symbols / lint.run share the resolver; a relative path
    # must land in the work_root, not the process CWD (the reported regression).
    import asyncio
    from tools.code.tree import CodeTree
    root = tmp_path / "ws"; (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.txt").write_text("hi")
    ctx = ToolContext(request_id="t", config={"tools": {}}, budget=None, work_root=str(root))
    r = asyncio.new_event_loop().run_until_complete(CodeTree().execute({"path": "sub"}, ctx))
    assert r.status == "ok"
    assert r.result["root"] == str(root / "sub")


def test_code_patch_tolerates_trailing_newline_marker(tmp_path):
    # Agent diffs commonly carry a wrong "\ No newline at end of file" marker.
    # Strict git apply rejects it; code.patch should retry whitespace-tolerant.
    import asyncio
    from tools.code.patch import CodePatch
    root = tmp_path / "ws"; root.mkdir()
    (root / "f.py").write_text("# test file\nx = 1\n")   # has trailing newline
    bad = ("--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n # test file\n"
           "-x = 1\n\\ No newline at end of file\n+x = 2\n")
    ctx = ToolContext(request_id="t", config={"tools": {}}, budget=None, work_root=str(root))
    r = asyncio.new_event_loop().run_until_complete(
        CodePatch().execute({"diff": bad, "base_dir": str(root)}, ctx))
    assert r.status == "ok" and (root / "f.py").read_text() == "# test file\nx = 2\n"


def test_code_patch_still_rejects_wrong_context(tmp_path):
    import asyncio
    from tools.code.patch import CodePatch
    root = tmp_path / "ws"; root.mkdir()
    (root / "f.py").write_text("# test file\nx = 1\n")
    wrong = "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n # test file\n-y = 99\n+y = 100\n"
    ctx = ToolContext(request_id="t", config={"tools": {}}, budget=None, work_root=str(root))
    r = asyncio.new_event_loop().run_until_complete(
        CodePatch().execute({"diff": wrong, "base_dir": str(root)}, ctx))
    assert r.status == "error"   # tolerance must not become blind fuzz
