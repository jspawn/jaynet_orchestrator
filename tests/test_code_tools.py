"""Tests for the code.* tools: run, patch, symbols, tree, deps."""
import subprocess

from conftest import run
from tools.code.run import CodeRun
from tools.code.patch import CodePatch
from tools.code.symbols import CodeSymbols
from tools.code.tree import CodeTree


def test_code_run_success(project, ctx):
    r = run(CodeRun().execute({"command": "echo hello", "cwd": str(project)}, ctx()))
    assert r.status == "ok" and r.result["exit_code"] == 0
    assert "hello" in r.result["stdout"] and r.result["ok"] is True


def test_code_run_nonzero_is_ok_status(project, ctx):
    # A failing command is a useful signal, not a tool error.
    r = run(CodeRun().execute({"command": "exit 7", "cwd": str(project)}, ctx()))
    assert r.status == "ok" and r.result["exit_code"] == 7 and r.result["ok"] is False


def test_code_run_confined(project, ctx):
    r = run(CodeRun().execute({"command": "ls", "cwd": "/etc"}, ctx(work_root=str(project))))
    assert r.status == "error" and "workspace" in r.error


def test_code_run_timeout(project, ctx):
    r = run(CodeRun().execute({"command": "sleep 5", "cwd": str(project),
                               "timeout_s": 1}, ctx()))
    assert r.result.get("timed_out") is True


def test_code_symbols_definitions(project, ctx):
    r = run(CodeSymbols().execute({"symbol": "greet", "mode": "definitions",
                                   "path": str(project)}, ctx()))
    assert r.status == "ok" and r.result["count"] >= 1
    assert any("app.py" in h["path"] for h in r.result["hits"])


def test_code_symbols_references(project, ctx):
    r = run(CodeSymbols().execute({"symbol": "greet", "mode": "references",
                                   "path": str(project)}, ctx()))
    assert r.result["count"] >= 2  # def + call site(s)


def test_code_symbols_rejects_non_identifier(project, ctx):
    r = run(CodeSymbols().execute({"symbol": "not a name!", "path": str(project)}, ctx()))
    assert r.status == "error"


def test_code_tree(project, ctx):
    r = run(CodeTree().execute({"path": str(project)}, ctx()))
    assert r.status == "ok" and "app.py" in r.result["tree"]
    assert "sub/" in r.result["tree"]


def test_code_patch_apply_and_dry_run(git_repo, ctx):
    # Produce a real unified diff via git, revert, then apply through the tool.
    (git_repo / "app.py").write_text('def greet(name):\n    return "hello " + name\n')
    diff = subprocess.run(["git", "-C", str(git_repo), "diff"],
                          capture_output=True, text=True).stdout
    subprocess.run(["git", "-C", str(git_repo), "checkout", "--", "app.py"])

    dry = run(CodePatch().execute({"diff": diff, "base_dir": str(git_repo),
                                   "dry_run": True}, ctx()))
    assert dry.status == "ok" and dry.result["dry_run"] is True

    applied = run(CodePatch().execute({"diff": diff, "base_dir": str(git_repo)}, ctx()))
    assert applied.status == "ok" and applied.result["applied"] is True
    assert "hello " in (git_repo / "app.py").read_text()


def test_code_patch_rejects_escape(project, ctx):
    bad = "--- a/../../etc/x\n+++ b/../../etc/x\n@@ -0,0 +1 @@\n+x\n"
    r = run(CodePatch().execute({"diff": bad, "base_dir": str(project)}, ctx()))
    assert r.status == "error" and "outside" in r.error
