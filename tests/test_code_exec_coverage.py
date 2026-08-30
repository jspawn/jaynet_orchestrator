"""code.run / code.execute (alias) / code.deps: sandbox command construction,
result parsing, container mode, venv/pip orchestration — all with the
subprocess layer mocked out.

No firejail, no uv/pip, no network: code.run gets a fake
asyncio.create_subprocess_exec, code.deps gets a fake _run. The sandbox workdir
is redirected to tmp_path via tools.code.workdir so nothing touches /srv.
"""
import asyncio
import os
import sys

import pytest

import tools.code.deps as DEPS
import tools.code.run as EX
from runtime.tool_base import ToolContext
from tools.code.deps import CodeDeps
from tools.code.execute import CodeExecute
from tools.code.run import CodeRun

# ------------------------------------------------------------------- code.run

class _Proc:
    def __init__(self, out=b"", err=b"", rc=0):
        self._out, self._err, self.returncode = out, err, rc
        self.killed = False

    async def communicate(self):
        return self._out, self._err

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


class _HangingProc(_Proc):
    async def communicate(self):
        await asyncio.sleep(60)
        return b"", b""


@pytest.fixture
def exec_ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ToolContext(request_id="t", budget=None, work_root=str(ws),
                       config={"tools": {"code": {
                           "workdir": str(tmp_path / "sandbox")}}})


def _patch_exec(monkeypatch, proc):
    """Fake asyncio.create_subprocess_exec; returns the recorded invocations."""
    calls = []

    async def fake_exec(*cmd, **kw):
        script = cmd[-1]
        # container mode passes a work_root-relative script name — read it
        # via the call's cwd when it isn't directly openable
        if script.endswith((".py", ".sh")) and not os.path.exists(script):
            script = os.path.join(kw.get("cwd") or ".", script)
        calls.append({"cmd": list(cmd), "cwd": kw.get("cwd"),
                      "script": open(script).read()
                      if script.endswith((".py", ".sh")) else None})
        return proc

    monkeypatch.setattr(EX.asyncio, "create_subprocess_exec", fake_exec)
    return calls


def test_firejail_command_construction(monkeypatch, exec_ctx, tmp_path):
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")
    calls = _patch_exec(monkeypatch, _Proc(out=b"hi\n"))
    r = asyncio.run(CodeRun().execute(
        {"command": "print('hi')", "language": "python"}, exec_ctx))
    assert r.status == "ok" and r.tool_name == "code.run"
    assert r.result["stdout"] == "hi\n" and r.result["stderr"] == ""
    assert r.result["exit_code"] == 0 and r.result["ok"] is True
    cmd = calls[0]["cmd"]
    assert cmd[0] == "firejail"
    for flag in ("--quiet", "--noprofile", "--net=none", "--private-tmp",
                 "--read-only=/", "--rlimit-as=1073741824", "--rlimit-cpu=60"):
        assert flag in cmd
    assert any(c.startswith("--private-cwd=") for c in cmd)
    assert any(c.startswith("--read-write=") for c in cmd)
    assert cmd[-2] == "python" and cmd[-1].endswith(".py")
    # the injected preamble ships with the snippet, and the per-call workdir
    # is cleaned up afterwards
    assert "sys.path" in calls[0]["script"] and "print('hi')" in calls[0]["script"]
    assert list((tmp_path / "sandbox").iterdir()) == []


def test_fallback_when_firejail_missing(monkeypatch, exec_ctx):
    monkeypatch.setattr(EX.shutil, "which", lambda name: None)
    calls = _patch_exec(monkeypatch, _Proc(out=b"ok\n"))
    r = asyncio.run(CodeRun().execute(
        {"command": "print('ok')", "language": "python"}, exec_ctx))
    assert r.status == "ok"
    cmd = calls[0]["cmd"]
    assert cmd[0] == "python" and cmd[1].endswith(".py")
    assert "firejail" not in cmd


def test_needs_confirmation_gates_missing_or_disabled_sandbox(monkeypatch, exec_ctx):
    # audit H1: bare execution must never be silent — the approval gate engages
    # when the sandbox can't actually run.
    tool = CodeRun()
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")
    assert tool.needs_confirmation(
        {"command": "x", "language": "python"}, exec_ctx) is False
    monkeypatch.setattr(EX.shutil, "which", lambda name: None)
    assert tool.needs_confirmation(
        {"command": "x", "language": "python"}, exec_ctx) is True
    # operator explicitly disabled the sandbox -> gated, like code.run's []
    ctx_off = ToolContext(request_id="t", budget=None,
                          config={"tools": {"code": {"sandbox": None}}})
    assert tool.needs_confirmation(
        {"command": "x", "language": "python"}, ctx_off) is True


def test_nonzero_exit_is_ok_with_output(monkeypatch, exec_ctx):
    # A non-zero exit is a normal signal (a failing test), not a tool error:
    # status stays "ok" and the payload carries ok/exit_code.
    monkeypatch.setattr(EX.shutil, "which", lambda name: None)
    _patch_exec(monkeypatch, _Proc(out=b"partial\n", err=b"boom\n", rc=3))
    r = asyncio.run(CodeRun().execute(
        {"command": "x", "language": "python"}, exec_ctx))
    assert r.status == "ok"
    assert r.result["ok"] is False and r.result["exit_code"] == 3
    assert r.result["stdout"] == "partial\n" and r.result["stderr"] == "boom\n"


def test_timeout_kills_process(monkeypatch, exec_ctx):
    # Python host timeout: the process group is killed and the payload says so
    # (timed_out + note + exit_code 124) — still status "ok", not a tool error.
    monkeypatch.setattr(EX.shutil, "which", lambda name: None)
    proc = _HangingProc()
    _patch_exec(monkeypatch, proc)
    r = asyncio.run(CodeRun().execute(
        {"command": "x", "language": "python", "timeout_s": 1}, exec_ctx))
    assert r.status == "ok"
    assert r.result["timed_out"] is True
    assert r.result["note"] == "killed after 1s"
    assert r.result["exit_code"] == 124 and r.result["ok"] is False
    assert proc.killed is True


# ------------------------------------------------------------- code.execute alias

def test_alias_maps_code_and_defaults_to_python(monkeypatch, exec_ctx):
    """The legacy code.execute alias maps code→command, defaults language to
    python (exercising the full merged machinery) and keeps its own name in
    the result."""
    assert CodeExecute().parameters["required"] == ["code"]
    assert CodeExecute().name == "code.execute"
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")
    calls = _patch_exec(monkeypatch, _Proc(out=b"hi\n"))
    r = asyncio.run(CodeExecute().execute({"code": "print('hi')"}, exec_ctx))
    assert r.status == "ok" and r.tool_name == "code.execute"
    # the python path was taken: firejail argv + import-scrub preamble
    assert calls[0]["cmd"][0] == "firejail"
    assert "sys.path" in calls[0]["script"] and "print('hi')" in calls[0]["script"]


def test_alias_gates_python_when_sandbox_disabled(monkeypatch, exec_ctx):
    """Audit #11 D2: the alias's raw args carry `code`, not `language` —
    gating must normalize to the PYTHON branch first, or a disabled python
    sandbox (tools.code.sandbox: null) silently ungates bare host python."""
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")
    exec_ctx.config["tools"]["code"]["sandbox"] = None
    assert CodeExecute().needs_confirmation({"code": "print(1)"}, exec_ctx) is True
    # bash calls in the same config are unaffected (their own branch rules)
    assert CodeRun().needs_confirmation({"command": "ls"}, exec_ctx) is False
    # default config: firejail present → python ungated
    exec_ctx.config["tools"]["code"].pop("sandbox")
    assert CodeExecute().needs_confirmation({"code": "print(1)"}, exec_ctx) is False


def test_python_default_timeout_reads_tools_code_key(monkeypatch, exec_ctx):
    """Audit #11 D3: tools.code.timeout_s is the python-mode default again
    (the pre-merge code.execute key config-help points at)."""
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")
    _patch_exec(monkeypatch, _Proc())
    exec_ctx.config["tools"]["code"]["timeout_s"] = 45
    seen = []
    real = EX.asyncio.wait_for

    async def wf(coro, timeout=None):
        seen.append(timeout)
        return await real(coro, timeout=timeout)

    monkeypatch.setattr(EX.asyncio, "wait_for", wf)
    r = asyncio.run(CodeRun().execute(
        {"command": "print(1)", "language": "python"}, exec_ctx))
    assert r.status == "ok"
    assert seen and seen[0] == 45
    # bash keeps its own default key
    exec_ctx.config["tools"]["code"]["run"] = {"timeout_s": 77}
    seen.clear()
    r = asyncio.run(CodeRun().execute({"command": "true"}, exec_ctx))
    assert seen and seen[0] == 77



# ------------------------------------------------------------------- code.deps

@pytest.fixture
def deps_fake(monkeypatch):
    """Fake code.deps._run: records argv, replays queued (rc, out, err)."""
    class Fake:
        def __init__(self):
            self.calls = []
            self.responses = []

        async def __call__(self, argv, cwd, timeout=300):
            self.calls.append(list(argv))
            return self.responses.pop(0) if self.responses else (0, "", "")

    fake = Fake()
    monkeypatch.setattr(DEPS, "_run", fake)
    return fake


def _touch_venv_python(project, venv_name=".venv"):
    py = project / venv_name / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.touch()
    return py


def test_deps_install_with_uv(project, ctx, deps_fake, monkeypatch):
    monkeypatch.setattr(DEPS.shutil, "which", lambda name: "/usr/bin/uv")
    r = asyncio.run(CodeDeps().execute(
        {"action": "install", "packages": ["httpx", "pytest>=8"]}, ctx()))
    assert r.status == "ok"
    venv = (project / ".venv").resolve()
    py = venv / "bin" / "python"
    # no venv yet -> created first, then the install runs against its python
    assert deps_fake.calls == [
        ["/usr/bin/uv", "venv", str(venv)],
        ["/usr/bin/uv", "pip", "install", "--python", str(py), "httpx", "pytest>=8"],
    ]
    assert r.result["tool"] == "uv" and r.result["installed"] == ["httpx", "pytest>=8"]


def test_deps_install_pip_fallback(project, ctx, deps_fake, monkeypatch):
    monkeypatch.setattr(DEPS.shutil, "which", lambda name: None)
    r = asyncio.run(CodeDeps().execute(
        {"action": "install", "packages": ["rich"]}, ctx()))
    assert r.status == "ok"
    venv = (project / ".venv").resolve()
    assert deps_fake.calls == [
        [sys.executable, "-m", "venv", str(venv)],
        [str(venv / "bin" / "python"), "-m", "pip", "install", "rich"],
    ]
    assert r.result["tool"] == "pip"


def test_deps_list_parses_freeze_output(project, ctx, deps_fake, monkeypatch):
    _touch_venv_python(project)
    monkeypatch.setattr(DEPS.shutil, "which", lambda name: None)
    deps_fake.responses = [(0, "httpx==0.28.1\npytest==8.0\n\n", "")]
    r = asyncio.run(CodeDeps().execute({"action": "list"}, ctx()))
    assert r.status == "ok"
    assert r.result["packages"] == ["httpx==0.28.1", "pytest==8.0"]
    assert r.result["count"] == 2
    py = (project / ".venv").resolve() / "bin" / "python"
    assert deps_fake.calls == [[str(py), "-m", "pip", "list", "--format=freeze"]]


def test_deps_list_requires_existing_venv(project, ctx, deps_fake, monkeypatch):
    monkeypatch.setattr(DEPS.shutil, "which", lambda name: None)
    r = asyncio.run(CodeDeps().execute({"action": "list"}, ctx()))
    assert r.status == "error" and "run action=create first" in r.error
    assert deps_fake.calls == []


def test_deps_install_needs_packages_or_requirements(project, ctx, deps_fake,
                                                     monkeypatch):
    _touch_venv_python(project)
    monkeypatch.setattr(DEPS.shutil, "which", lambda name: None)
    r = asyncio.run(CodeDeps().execute({"action": "install"}, ctx()))
    assert r.status == "error" and "packages" in r.error
    assert deps_fake.calls == []


def test_deps_rejects_escapes(project, ctx, deps_fake, monkeypatch):
    _touch_venv_python(project)
    monkeypatch.setattr(DEPS.shutil, "which", lambda name: None)
    r = asyncio.run(CodeDeps().execute(
        {"action": "create", "venv_name": "../outside"}, ctx()))
    assert r.status == "error" and "inside the project" in r.error
    r = asyncio.run(CodeDeps().execute(
        {"action": "install", "requirements": "../reqs.txt"}, ctx()))
    assert r.status == "error" and "escapes the project" in r.error
    assert deps_fake.calls == []


def test_out_dir_mounted_and_artifacts_returned(monkeypatch, exec_ctx, tmp_path):
    from pathlib import Path
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")
    calls = []

    async def fake_exec(*cmd, **kw):
        # simulate the snippet writing a chart into ORCH_EXEC_OUT
        out = Path(kw["env"]["ORCH_EXEC_OUT"])
        (out / "chart.png").write_bytes(b"png")
        calls.append({"cmd": list(cmd), "env": kw["env"]})
        return _Proc(out=b"ok\n")

    monkeypatch.setattr(EX.asyncio, "create_subprocess_exec", fake_exec)
    ws = tmp_path / "ws"
    r = asyncio.run(CodeRun().execute(
        {"command": "plot", "language": "python"}, exec_ctx))
    assert r.status == "ok"
    assert len(r.result["written_files"]) == 1
    assert r.result["written_files"][0].endswith("chart.png")
    assert r.result["out_dir"].startswith(str(ws / "exec-out"))
    rw = [c for c in calls[0]["cmd"] if c.startswith("--read-write=")]
    assert len(rw) == 3          # workdir + artifact dir + persistent workspace
    assert f"--read-write={ws}/exec-work" in rw
    assert calls[0]["env"]["MPLBACKEND"] == "Agg"


def test_no_work_root_no_artifacts(monkeypatch, tmp_path):
    # No work_root (CLI-style ctx with fs.allowed_roots only): the snippet
    # still runs, but there is no artifact channel.
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")
    _patch_exec(monkeypatch, _Proc(out=b"hi\n"))
    ctx = ToolContext(request_id="t", budget=None,
                      config={"tools": {
                          "code": {"workdir": str(tmp_path / "sandbox")},
                          "fs": {"allowed_roots": [str(tmp_path)]}}})
    r = asyncio.run(CodeRun().execute(
        {"command": "print('hi')", "language": "python"}, ctx))
    assert r.status == "ok"
    assert "out_dir" not in r.result and "written_files" not in r.result


# ------------------------------------------------------- container mode (eval)

@pytest.fixture
def ctr_ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ToolContext(request_id="t", budget=None, work_root=str(ws),
                       config={"tools": {"code": {
                           "workdir": str(tmp_path),
                           "container": {"id": "ctr-1", "workdir": "/app"}}}})


def test_container_command_construction(monkeypatch, ctr_ctx, tmp_path):
    """Container mode (eval runner's tools_patch): the snippet runs via
    podman exec inside the case container; the script lives under work_root
    (bind-mounted at the container workdir) and is removed afterwards."""
    calls = _patch_exec(monkeypatch, _Proc(out=b"hi\n"))
    r = asyncio.run(CodeRun().execute(
        {"command": "print('hi')", "language": "python"}, ctr_ctx))
    assert r.status == "ok" and r.result["stdout"] == "hi\n"
    assert r.result["ok"] is True and r.result["exit_code"] == 0
    assert r.result["sandbox"] == "container" and r.result["container"] == "ctr-1"
    cmd = calls[0]["cmd"]
    assert cmd[:3] == ["podman", "exec", "--workdir"]
    assert cmd[3] == "/app"
    envs = [cmd[i + 1] for i, c in enumerate(cmd) if c == "--env"]
    assert any(e.startswith("ORCH_EXEC_OUT=/app/exec-out/") for e in envs)
    assert "MPLBACKEND=Agg" in envs
    assert cmd[-5] == "ctr-1"
    assert cmd[-4:-2] == ["timeout", "120"]
    assert cmd[-2] == "python3" and cmd[-1].endswith(".py")
    # the script was written under the work root (visible in the container)
    # and deleted in finally; no firejail involved anywhere
    assert "firejail" not in cmd
    assert list((tmp_path / "ws").glob(".orch-exec-*.py")) == []
    # artifacts collection stays host-side under the same exec-out layout
    assert r.result["out_dir"].startswith(str(tmp_path / "ws" / "exec-out"))


def test_container_python_override_and_custom_workdir(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ToolContext(request_id="t", budget=None, work_root=str(ws),
                      config={"tools": {"code": {
                          "container": {"id": "c9", "workdir": "/task",
                                        "python": "python3.12"}}}})
    calls = _patch_exec(monkeypatch, _Proc(out=b"ok\n"))
    r = asyncio.run(CodeRun().execute(
        {"command": "print(1)", "language": "python", "timeout_s": 10}, ctx))
    assert r.status == "ok"
    cmd = calls[0]["cmd"]
    assert cmd[3] == "/task"
    assert cmd[-4:-2] == ["timeout", "10"]
    assert cmd[-2] == "python3.12"


def test_container_needs_confirmation_is_false(monkeypatch, ctr_ctx):
    # The container IS the sandbox: no firejail needed on the host, and the
    # call is never confirmation-gated (keeps code.run in the eval toolset on
    # hosts without firejail).
    monkeypatch.setattr(EX.shutil, "which", lambda name: None)
    assert CodeRun().needs_confirmation({"command": "x"}, ctr_ctx) is False


def test_container_requires_work_root(tmp_path):
    ctx = ToolContext(request_id="t", budget=None,
                      config={"tools": {"code": {
                          "workdir": str(tmp_path),
                          "container": {"id": "c1"}}}})
    r = asyncio.run(CodeRun().execute({"command": "x"}, ctx))
    assert r.status == "error" and "work_root" in r.error


def test_container_nonzero_exit(monkeypatch, ctr_ctx):
    # Same unified contract as the host paths: a non-zero exit is status "ok"
    # with the failure in the payload — only a timeout kill is a tool error.
    _patch_exec(monkeypatch, _Proc(out=b"p\n", err=b"boom\n", rc=2))
    r = asyncio.run(CodeRun().execute(
        {"command": "x", "language": "python"}, ctr_ctx))
    assert r.status == "ok"
    assert r.result["ok"] is False and r.result["exit_code"] == 2
    assert r.result["stderr"] == "boom\n"
    assert r.result["sandbox"] == "container"


def test_container_skips_subcall_grant(monkeypatch, ctr_ctx):
    """The llm_query seam can't cross into a running container — the grant
    must not even be requested (and the snippet still runs)."""
    asked = []

    async def grant(spec):
        asked.append(spec)
        return {"sock": "/s", "token": "t", "used": 0, "max_calls": 4}

    ctr_ctx.subcall_grant = grant
    calls = _patch_exec(monkeypatch, _Proc(out=b"hi\n"))
    r = asyncio.run(CodeRun().execute(
        {"command": "print(1)", "language": "python"}, ctr_ctx))
    assert r.status == "ok"
    assert asked == []
    envs = [c for c in calls[0]["cmd"] if "ORCH_SUBCALL" in c]
    assert envs == []


# --------------------------- persistent workspace, bash, output spill ---------

def test_persistent_workspace_env_and_mount(monkeypatch, tmp_path):
    """Runs with a work_root get a PERSISTENT exec workspace: env carries
    ORCH_EXEC_WORK and the firejail cmd bind-mounts it (the same mechanism
    that lets the artifact dir survive --private-tmp)."""
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")
    captured = []

    async def fake_exec(*cmd, **kw):
        captured.append({"cmd": list(cmd), "env": kw["env"],
                         "script": open(cmd[-1]).read()})
        return _Proc(out=b"ok\n")

    monkeypatch.setattr(EX.asyncio, "create_subprocess_exec", fake_exec)
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ToolContext(request_id="t", budget=None, work_root=str(ws),
                      config={"tools": {"code": {"workdir": str(tmp_path)}}})
    r = asyncio.run(CodeRun().execute(
        {"command": "print('ok')", "language": "python"}, ctx))
    assert r.status == "ok"
    assert f"--read-write={ws}/exec-work" in [
        c for c in captured[0]["cmd"] if c.startswith("--read-write=")]
    assert captured[0]["env"]["ORCH_EXEC_WORK"] == str(ws / "exec-work")
    assert "ORCH_EXEC_WORK" in captured[0]["script"]     # preamble chdir


def test_persistent_workspace_real_exec(tmp_path):
    """End-to-end with the real firejail: a file written in call 1 is readable
    in call 2 — the /tmp bind actually works (this was the exit-1 regression
    class; /tmp binds need the --whitelist companion). Uses the default
    SANDBOX_DIR base: the per-call workdir base must be non-/tmp by design."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # The per-call workdir base must be non-/tmp by design (--private-cwd
    # under /tmp is hidden by --private-tmp — the original exit-1
    # regression). conftest points SANDBOX_DIR at /tmp, so anchor at the repo
    # root instead: writable in CI, unlike paths.HOME (defaults to
    # /srv/orchestrator when ORCH_HOME is unset).
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent / "data" / "fj-test"
    base.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(request_id="t", budget=None, work_root=str(ws),
                      config={"tools": {"code": {"workdir": str(base)}}})
    tool = CodeRun()
    r1 = asyncio.run(tool.execute(
        {"command": "open('state.txt', 'w').write('42')",
         "language": "python"}, ctx))
    assert r1.status == "ok", r1.error
    assert (ws / "exec-work" / "state.txt").read_text() == "42"
    r2 = asyncio.run(tool.execute(
        {"command": "print(open('state.txt').read())",
         "language": "python"}, ctx))
    assert r2.status == "ok" and r2.result["stdout"].strip() == "42"


def test_container_bash_language(monkeypatch, ctr_ctx, tmp_path):
    """Benchmark tasks are CLI-native: language=bash (the default) runs the
    snippet via bash in the container (no Python preamble)."""
    calls = _patch_exec(monkeypatch, _Proc(out=b"hi\n"))
    r = asyncio.run(CodeRun().execute({"command": "echo hi"}, ctr_ctx))
    assert r.status == "ok"
    cmd = calls[0]["cmd"]
    assert cmd[-2] == "bash" and cmd[-1].endswith(".sh")
    assert list((tmp_path / "ws").glob(".orch-exec-*.sh")) == []   # cleaned


def test_stdout_spill_to_file(monkeypatch, tmp_path):
    """Past the inline cap the FULL output is written to the artifact dir and
    the path travels in the result — truncation never silently drops output."""
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")
    _patch_exec(monkeypatch, _Proc(out=("x" * 6000 + "\n").encode()))
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ToolContext(request_id="t", budget=None, work_root=str(ws),
                      config={"tools": {"code": {"workdir": str(tmp_path)}}})
    r = asyncio.run(CodeRun().execute(
        {"command": "print('x' * 6000)", "language": "python"}, ctx))
    assert r.status == "ok"
    assert len(r.result["stdout"]) == 5000
    spill = r.result["stdout_file"]
    assert spill.startswith(str(ws / "exec-out"))
    assert open(spill).read() == "x" * 6000 + "\n"


def test_spill_streams_unit(tmp_path):
    assert EX._spill_streams("short", "", tmp_path) == {}
    assert EX._spill_streams("x" * 6000, "", None) == {}   # no artifact dir
    files = EX._spill_streams("y" * 6001, "e" * 2000, tmp_path)
    assert set(files) == {"stdout_file", "stderr_file"}
    assert open(files["stderr_file"]).read() == "e" * 2000


def test_spill_files_not_in_written_files(monkeypatch, tmp_path):
    """Audit D2: spill files travel via stdout_file/stderr_file keys — they
    must NOT pollute written_files (the deliver-these artifacts list)."""
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")

    async def fake_exec(*cmd, **kw):
        from pathlib import Path as P
        (P(kw["env"]["ORCH_EXEC_OUT"]) / "chart.png").write_bytes(b"png")
        return _Proc(out=("x" * 6000).encode())

    monkeypatch.setattr(EX.asyncio, "create_subprocess_exec", fake_exec)
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ToolContext(request_id="t", budget=None, work_root=str(ws),
                      config={"tools": {"code": {"workdir": str(tmp_path)}}})
    r = asyncio.run(CodeRun().execute(
        {"command": "plot", "language": "python"}, ctx))
    assert r.status == "ok"
    assert [f for f in r.result["written_files"] if f.endswith("chart.png")]
    assert not any(f.endswith("stdout.txt")
                   for f in r.result["written_files"])
    assert r.result["stdout_file"].endswith("stdout.txt")
