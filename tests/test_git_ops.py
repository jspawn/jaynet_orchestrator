"""Tests for git remote/working-tree ops and worktrees."""
import subprocess

import pytest

from conftest import run
from tools.git.remote import GitFetch, GitPull, GitPush, GitStash, GitRestore
from tools.git.status import GitBranch, GitDiff, GitShow
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


def test_fetch_rejects_option_remote(repo_with_remote, ctx, tmp_path):
    # git.fetch is not confirmation-gated; --upload-pack would run a local command.
    repo, bare = repo_with_remote
    pwned = tmp_path / "pwned"
    r = run(GitFetch().execute({"remote": f"--upload-pack=touch {pwned}"},
                               ctx(config=_cfg(repo, bare))))
    assert r.status == "error"
    assert not pwned.exists()  # nothing was executed


def test_pull_push_reject_option_args(repo_with_remote, ctx):
    repo, bare = repo_with_remote
    c = lambda: ctx(config=_cfg(repo, bare))
    assert run(GitPull().execute({"remote": "--upload-pack=x"}, c())).status == "error"
    assert run(GitPull().execute({"remote": "origin", "branch": "-x"}, c())).status == "error"
    assert run(GitPush().execute({"remote": "--upload-pack=x"}, c())).status == "error"
    assert run(GitPush().execute({"branch": "--delete"}, c())).status == "error"


def test_pull_push_reject_url_remote(repo_with_remote, ctx, tmp_path):
    # Like fetch: pull/push take a configured remote NAME only — an ext::
    # transport in that slot is command execution, a URL an unvetted connection.
    repo, bare = repo_with_remote
    c = lambda: ctx(config=_cfg(repo, bare))
    pwned = tmp_path / "pwned"
    evil = [f"ext::sh -c 'touch {pwned}'", "https://evil.example/r.git",
            "git@evil.example:r.git"]
    for tool in (GitFetch(), GitPull(), GitPush()):
        for remote in evil:
            r = run(tool.execute({"remote": remote}, c()))
            assert r.status == "error" and "unsafe remote" in r.error
    assert not pwned.exists()  # nothing was executed


def test_pull_push_reject_unknown_remote(repo_with_remote, ctx):
    repo, bare = repo_with_remote
    c = lambda: ctx(config=_cfg(repo, bare))
    r = run(GitPull().execute({"remote": "nosuch"}, c()))
    assert r.status == "error" and "unknown remote" in r.error
    r = run(GitPush().execute({"remote": "nosuch"}, c()))
    assert r.status == "error" and "unknown remote" in r.error


def test_diff_show_reject_option_ref(git_repo, ctx, tmp_path):
    # --output=<path> would clobber an arbitrary file with diff output.
    pwned = tmp_path / "pwned"
    c = lambda: ctx(config=_cfg(git_repo))
    assert run(GitDiff().execute({"ref": f"--output={pwned}"}, c())).status == "error"
    assert run(GitShow().execute({"ref": f"--output={pwned}"}, c())).status == "error"
    assert not pwned.exists()


def test_branch_rejects_option_name(git_repo, ctx):
    r = run(GitBranch().execute({"name": "-d", "create": True},
                                ctx(config=_cfg(git_repo))))
    assert r.status == "error"


def test_worktree_rejects_option_branch(git_repo, ctx, tmp_path):
    wt = tmp_path / "wt-x"
    r = run(GitWorktree().execute({"action": "add", "path": str(wt), "branch": "--force"},
                                  ctx(config=_cfg(git_repo, tmp_path))))
    assert r.status == "error"
    assert not wt.exists()


def test_diff_show_ref_still_work(git_repo, ctx):
    c = lambda: ctx(config=_cfg(git_repo))
    (git_repo / "app.py").write_text("changed\n")
    r = run(GitDiff().execute({"ref": "HEAD"}, c()))
    assert r.status == "ok" and "changed" in r.result["diff"]
    r = run(GitShow().execute({"ref": "HEAD"}, c()))
    assert r.status == "ok" and "init" in r.result["show"]


def test_branch_normal_flow_still_works(git_repo, ctx):
    c = lambda: ctx(config=_cfg(git_repo))
    r = run(GitBranch().execute({}, c()))
    assert r.status == "ok" and "main" in r.result["branches"]
    r = run(GitBranch().execute({"name": "feat", "create": True}, c()))
    assert r.status == "ok" and r.result["switched_to"] == "feat"


# ---- history + repo-level ops (blame/merge/tag/reset/clone) ----
from tools.git.history import GitBlame, GitMerge, GitTag, GitReset, GitClone


def _git_run(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_blame_shows_authorship(git_repo, ctx):
    c = lambda: ctx(config=_cfg(git_repo))
    r = run(GitBlame().execute({"path": "app.py"}, c()))
    assert r.status == "ok" and r.result["blame"].strip()
    r = run(GitBlame().execute({"path": "app.py", "start_line": 1, "end_line": 1}, c()))
    assert len(r.result["blame"].splitlines()) == 1


def test_tag_roundtrip_and_dynamic_gating(git_repo, ctx):
    c = lambda: ctx(config=_cfg(git_repo))
    t = GitTag()
    assert t.needs_confirmation({"action": "list"}, None) is False
    assert t.needs_confirmation({"action": "create"}, None) is True
    assert t.needs_confirmation({"action": "delete"}, None) is True
    assert run(t.execute({"action": "create", "name": "v1", "message": "first"}, c())).status == "ok"
    assert run(t.execute({"action": "list"}, c())).result["tags"] == ["v1"]
    assert run(t.execute({"action": "delete", "name": "v1"}, c())).status == "ok"
    assert run(t.execute({"action": "list"}, c())).result["tags"] == []


def test_merge_no_ff(git_repo, ctx):
    c = lambda: ctx(config=_cfg(git_repo))
    _git_run(git_repo, "switch", "-c", "feat")
    (git_repo / "feat.py").write_text("x = 1\n")
    _git_run(git_repo, "add", "-A")
    _git_run(git_repo, "commit", "-m", "feat work")
    _git_run(git_repo, "switch", "-")
    r = run(GitMerge().execute({"ref": "feat", "no_ff": True}, c()))
    assert r.status == "ok" and (git_repo / "feat.py").exists()
    out = subprocess.run(["git", "-C", str(git_repo), "log", "--pretty=%s", "-n1"],
                         capture_output=True, text=True).stdout
    assert "Merge" in out


def test_reset_hard_discards(git_repo, ctx):
    c = lambda: ctx(config=_cfg(git_repo))
    (git_repo / "tmp.txt").write_text("v1\n")
    _git_run(git_repo, "add", "-A")
    _git_run(git_repo, "commit", "-m", "add tmp")
    (git_repo / "tmp.txt").write_text("v2\n")
    r = run(GitReset().execute({"mode": "hard", "ref": "HEAD"}, c()))
    assert r.status == "ok" and (git_repo / "tmp.txt").read_text() == "v1\n"


def test_reset_paths_unstages(git_repo, ctx):
    c = lambda: ctx(config=_cfg(git_repo))
    (git_repo / "st.py").write_text("s\n")
    _git_run(git_repo, "add", "st.py")
    r = run(GitReset().execute({"paths": ["st.py"]}, c()))
    assert r.status == "ok"
    out = subprocess.run(["git", "-C", str(git_repo), "status", "--porcelain"],
                         capture_output=True, text=True).stdout
    assert out.startswith("??")          # back to untracked


def test_clone_local_inside_workspace(git_repo, ctx, tmp_path):
    src = tmp_path / "src"
    subprocess.run(["git", "clone", "-q", str(git_repo), str(src)], check=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    c = lambda: ctx(config=_cfg(ws, src))
    r = run(GitClone().execute({"url": str(src), "dest": "copy"}, c()))
    assert r.status == "ok" and (ws / "copy" / "app.py").exists()


def test_clone_dest_confinement(git_repo, ctx, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run(GitClone().execute({"url": "https://x/y.git", "dest": "/etc/evil"},
                               ctx(config=_cfg(ws))))
    assert r.status == "error" and "outside your workspace" in r.error


def test_clone_source_confinement(git_repo, ctx, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run(GitClone().execute({"url": "/etc", "dest": "x"}, ctx(config=_cfg(ws))))
    assert r.status == "error" and "outside your workspace" in r.error
