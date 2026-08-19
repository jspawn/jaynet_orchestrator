"""Tests for the code.* tools: run, patch, symbols, tree, deps."""
import subprocess

from conftest import run

from tools.code.patch import CodePatch
from tools.code.run import CodeRun
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


def test_scrub_env_rule():
    # Denylist + *_KEY/*_TOKEN/*_SECRET/*_PASSWORD (+ _PASSPHRASE/_PAT/_DSN)
    # suffixes are dropped; normal tooling vars survive. The rule lives in
    # runtime.tool_base so code.run, the verifier's check command and the
    # serving layer share it.
    from runtime.tool_base import scrub_env
    env = {"PATH": "/bin", "HOME": "/h", "LANG": "C", "EDITOR": "vim",
           "LITELLM_MASTER_KEY": "x", "TAVILY_API_KEY": "y", "SOME_TOKEN": "z",
           "DB_PASSWORD": "w", "APP_SECRET": "v",
           "SSH_PASSPHRASE": "p", "GITHUB_PAT": "q", "PG_DSN": "r",
           "DATABASE_URL": "postgres://u:p@h/db"}
    assert scrub_env(env) == {"PATH": "/bin", "HOME": "/h", "LANG": "C",
                              "EDITOR": "vim"}


def test_launch_server_scrubs_secret_env(tmp_path, monkeypatch):
    """Audit B14: serving launches no longer hand llama-server the master's
    secrets in /proc/<pid>/environ."""
    monkeypatch.setenv("LITELLM_MASTER_KEY", "topsecret")
    monkeypatch.setenv("JAYNET_WEB_TOKEN", "alsotoken")
    from runtime import serving
    info = serving.launch_server(tmp_path, "t", "env > env.out",
                                 cwd=str(tmp_path), gpu=None,
                                 source_env=False, env_setup=None)
    for _ in range(50):
        if not serving.pid_alive(info["pid"]):
            break
        import time
        time.sleep(0.05)
    dumped = (tmp_path / "env.out").read_text()
    assert "topsecret" not in dumped and "alsotoken" not in dumped
    assert "GPU_MAX_HW_QUEUES=1" in dumped   # functional vars survive


def test_code_run_does_not_leak_secret_env(project, ctx, monkeypatch):
    # A model-influenced command must not see the orchestrator's API keys.
    monkeypatch.setenv("TAVILY_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MY_CUSTOM_TOKEN", "also-secret")
    r = run(CodeRun().execute(
        {"command": "echo K=${TAVILY_API_KEY:-unset} T=${MY_CUSTOM_TOKEN:-unset} "
                    "P=${PATH:+set}", "cwd": str(project)}, ctx()))
    assert r.status == "ok" and "K=unset T=unset P=set" in r.result["stdout"]


def test_code_run_caller_env_still_passes_through(project, ctx):
    # Explicit per-call env is applied after the scrub, so it still works.
    r = run(CodeRun().execute({"command": "echo V=$FOO", "cwd": str(project),
                               "env": {"FOO": "bar"}}, ctx()))
    assert r.status == "ok" and "V=bar" in r.result["stdout"]
