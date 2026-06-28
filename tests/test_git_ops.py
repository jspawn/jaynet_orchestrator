"""Tests for git remote/working-tree ops and worktrees."""
import subprocess

import pytest

from conftest import run
from tools.git.remote import GitFetch, GitPull, GitPush, GitStash, GitRestore
from tools.git.worktree import GitWorktree


@pytest.fixture
def repo_with_remote(git_repo, tmp_path):
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", str(git_repo), "remote", "add", "origin", str(bare)],
                   check=True)
    return git_repo, bare


def _cfg(git_repo, *extra_roots):
    roots = [str(git_repo)] + [str(e) for e in extra_roots]
    return {"tools": {"git": {"allowed_roots": roots, "default_repo": str(git_repo)}},
            "trace": {}}


def test_push_fetch_pull(repo_with_remote, ctx):
    repo, bare = repo_with_remote
    c = lambda: ctx(config=_cfg(repo, bare))
    r = run(GitPush().execute({"set_upstream": True, "branch": "main"}, c()))
    assert r.status == "ok"
    r = run(GitFetch().execute({}, c()))
    assert r.status == "ok"
    r = run(GitPull().execute({}, c()))
    assert r.status == "ok"


def test_stash_roundtrip(git_repo, ctx):
    c = lambda: ctx(config=_cfg(git_repo))
    (git_repo / "app.py").write_text("dirty\n")
    r = run(GitStash().execute({"action": "push", "message": "wip"}, c()))
    assert r.status == "ok"
    assert (git_repo / "app.py").read_text() != "dirty\n"  # tree cleaned
    r = run(GitStash().execute({"action": "list"}, c()))
    assert len(r.result["stashes"]) == 1
    r = run(GitStash().execute({"action": "pop"}, c()))
    assert (git_repo / "app.py").read_text() == "dirty\n"


def test_restore_discards(git_repo, ctx):
    c = lambda: ctx(config=_cfg(git_repo))
    original = (git_repo / "app.py").read_text()
    (git_repo / "app.py").write_text("dirty\n")
    r = run(GitRestore().execute({"paths": ["app.py"]}, c()))
    assert r.status == "ok"
    assert (git_repo / "app.py").read_text() == original


def test_git_confinement(git_repo, ctx):
    r = run(GitFetch().execute({"repo": "/etc"}, ctx(config=_cfg(git_repo))))
    assert r.status == "error" and "workspace" in r.error


def test_worktree_add_list_remove(git_repo, ctx, tmp_path):
    wt = tmp_path / "wt-feat"
    c = lambda: ctx(config=_cfg(git_repo, tmp_path))
    r = run(GitWorktree().execute({"action": "add", "path": str(wt),
                                   "branch": "feat", "create_branch": True}, c()))
    assert r.status == "ok" and wt.exists()
    assert (wt / "app.py").exists()  # the worktree is a real checkout

    r = run(GitWorktree().execute({"action": "list"}, c()))
    paths = [w.get("path") for w in r.result["worktrees"]]
    assert any(str(wt) in (p or "") for p in paths)

    r = run(GitWorktree().execute({"action": "remove", "path": str(wt)}, c()))
    assert r.status == "ok" and not wt.exists()


def test_worktree_dest_confined(git_repo, ctx):
    # default config only allows git_repo as a root; /tmp/elsewhere is out.
    r = run(GitWorktree().execute({"action": "add", "path": "/tmp/elsewhere-xyz",
                                   "branch": "x", "create_branch": True},
                                  ctx(config=_cfg(git_repo))))
    assert r.status == "error" and "workspace" in r.error
