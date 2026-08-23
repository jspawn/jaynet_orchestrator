"""fs.write/fs.edit fire on_project_file_changed in project-bound runs.

The web API already fired this hook; the agent's own fs.* writes did not, so
project-scoped plugin state (graphify's graph) went stale after agent-heavy
runs. The hook must receive the RESOLVED projects root derived from work_root,
and must stay silent for non-project runs and scratch (tmp_root) writes."""

from __future__ import annotations

import pytest
from conftest import run

from runtime import hooks
from tools.fs.ops import FsEdit, FsWrite


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear()
    yield
    hooks.clear()


@pytest.fixture
def project_tree(tmp_path):
    """A projects_dir/<owner>/<pid>/files layout like web.projects.files_root."""
    files = tmp_path / "projects" / "alice" / "p1" / "files"
    files.mkdir(parents=True)
    return tmp_path / "projects", files


def _capture():
    seen = []
    hooks.register("on_project_file_changed",
                   lambda o, p, path, pdir: seen.append((o, p, path, pdir)))
    return seen


def test_write_fires_hook_with_resolved_projects_dir(ctx, project_tree):
    projects_dir, files = project_tree
    seen = _capture()
    c = ctx(owner="alice", project_id="p1", work_root=str(files))
    res = run(FsWrite().execute({"path": "a.txt", "content": "hi"}, c))
    assert res.status == "ok"
    assert len(seen) == 1
    owner, pid, path, pdir = seen[0]
    assert (owner, pid) == ("alice", "p1")
    assert path.endswith("a.txt")
    # The resolved projects root, three levels up from the files dir — correct
    # even when web.projects_dir points somewhere non-default.
    assert pdir == str(projects_dir.resolve())


def test_edit_fires_hook(ctx, project_tree):
    _, files = project_tree
    seen = _capture()
    c = ctx(owner="alice", project_id="p1", work_root=str(files))
    run(FsWrite().execute({"path": "f.txt", "content": "one two"}, c))
    res = run(FsEdit().execute({"path": "f.txt", "old_str": "two", "new_str": "TWO"}, c))
    assert res.status == "ok"
    assert len(seen) == 2                       # one fire per successful write


def test_no_fire_without_project(ctx):
    seen = _capture()
    res = run(FsWrite().execute({"path": "x.txt", "content": "y"}, ctx()))
    assert res.status == "ok"
    assert seen == []


def test_no_fire_for_tmp_root_write(ctx, project_tree, tmp_path):
    _, files = project_tree
    seen = _capture()
    scratch = tmp_path / "run-scratch"
    c = ctx(owner="alice", project_id="p1", work_root=str(files),
            tmp_root=str(scratch))
    res = run(FsWrite().execute(
        {"path": str(scratch / "note.txt"), "content": "y"}, c))
    assert res.status == "ok"
    assert seen == []                           # ephemeral scratch is not the project
