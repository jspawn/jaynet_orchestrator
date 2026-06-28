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
