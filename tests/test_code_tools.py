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


def test_code_patch_recounts_model_hunk_headers(git_repo, ctx):
    """Local models are off-by-one in @@ counts (seen in live selftests):
    the hunk BODY is right, the header's old-side count is not. --recount
    recomputes it; context must still match exactly. (The live shape was a
    report append: a hunks that reaches EOF needs no trailing context —
    mid-file hunks without trailing context are unanchorable for git apply
    and stay refused.)"""
    lines = ["# report", "", "## Tier 1", "done",
             "Chained fs → rag → memory → kg → pdf.", ""]
    (git_repo / "report.md").write_text("\n".join(lines) + "\n")
    # Header claims -5,3 (old side 3 lines) but the body has only 2 context
    # lines — git apply strict rejects this as "corrupt patch".
    diff = ("--- a/report.md\n+++ b/report.md\n"
            "@@ -5,3 +5,5 @@\n"
            " Chained fs → rag → memory → kg → pdf.\n"
            " \n"
            "+## Tier 3 — service-gated\n"
            "+Validated.\n"
            "+\n")
    r = run(CodePatch().execute({"diff": diff, "base_dir": str(git_repo)}, ctx()))
    assert r.status == "ok", r.error
    assert "recount" in r.result["message"]
    out = (git_repo / "report.md").read_text()
    assert "## Tier 3 — service-gated" in out and out.endswith("Validated.\n\n")


def test_code_patch_recount_does_not_save_wrong_context(git_repo, ctx):
    """--recount fixes counts, not context: a hunk whose context lines don't
    match must still fail."""
    (git_repo / "report.md").write_text("actual content\n")
    diff = ("--- a/report.md\n+++ b/report.md\n"
            "@@ -1,3 +1,4 @@\n"
            " something else entirely\n"
            "+added\n")
    r = run(CodePatch().execute({"diff": diff, "base_dir": str(git_repo)}, ctx()))
    assert r.status == "error"
    assert (git_repo / "report.md").read_text() == "actual content\n"


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
