"""code.execute / code.deps: sandbox command construction, result parsing,
venv/pip orchestration — all with the subprocess layer mocked out.

No firejail, no uv/pip, no network: code.execute gets a fake
asyncio.create_subprocess_exec, code.deps gets a fake _run. The sandbox workdir
is redirected to tmp_path via tools.code.workdir so nothing touches /srv.
"""
import asyncio
import sys

import pytest

import tools.code.execute as EX
import tools.code.deps as DEPS
from tools.code.execute import CodeExecute
from tools.code.deps import CodeDeps
from runtime.tool_base import ToolContext


# ---------------------------------------------------------------- code.execute

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
    return ToolContext(request_id="t", budget=None,
                       config={"tools": {"code": {"workdir": str(tmp_path)}}})


def _patch_exec(monkeypatch, proc):
    """Fake asyncio.create_subprocess_exec; returns the recorded invocations."""
    calls = []

    async def fake_exec(*cmd, **kw):
        script = cmd[-1]
        calls.append({"cmd": list(cmd), "cwd": kw.get("cwd"),
                      "script": open(script).read() if script.endswith(".py") else None})
        return proc

    monkeypatch.setattr(EX.asyncio, "create_subprocess_exec", fake_exec)
    return calls


def test_firejail_command_construction(monkeypatch, exec_ctx, tmp_path):
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")
    calls = _patch_exec(monkeypatch, _Proc(out=b"hi\n"))
    r = asyncio.run(CodeExecute().execute({"code": "print('hi')"}, exec_ctx))
    assert r.status == "ok"
    assert r.result == {"stdout": "hi\n", "stderr": "", "exit_code": 0}
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
    assert list(tmp_path.iterdir()) == []


def test_fallback_when_firejail_missing(monkeypatch, exec_ctx):
    monkeypatch.setattr(EX.shutil, "which", lambda name: None)
    calls = _patch_exec(monkeypatch, _Proc(out=b"ok\n"))
    r = asyncio.run(CodeExecute().execute({"code": "print('ok')"}, exec_ctx))
    assert r.status == "ok"
    cmd = calls[0]["cmd"]
    assert cmd[0] == "python" and cmd[1].endswith(".py")
    assert "firejail" not in cmd


def test_nonzero_exit_is_error_with_output(monkeypatch, exec_ctx):
    monkeypatch.setattr(EX.shutil, "which", lambda name: None)
    _patch_exec(monkeypatch, _Proc(out=b"partial\n", err=b"boom\n", rc=3))
    r = asyncio.run(CodeExecute().execute({"code": "x"}, exec_ctx))
    assert r.status == "error" and r.error == "exit code 3"
    assert r.result["exit_code"] == 3
    assert r.result["stdout"] == "partial\n" and r.result["stderr"] == "boom\n"


def test_timeout_kills_process(monkeypatch, exec_ctx):
    monkeypatch.setattr(EX.shutil, "which", lambda name: None)
    proc = _HangingProc()
    _patch_exec(monkeypatch, proc)
    r = asyncio.run(CodeExecute().execute({"code": "x", "timeout_s": 1}, exec_ctx))
    assert r.status == "error" and "execution timeout after 1s" in r.error
    assert proc.killed is True


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
    ws.mkdir()
    exec_ctx.work_root = str(ws)
    r = asyncio.run(CodeExecute().execute({"code": "plot"}, exec_ctx))
    assert r.status == "ok"
    assert len(r.result["written_files"]) == 1
    assert r.result["written_files"][0].endswith("chart.png")
    assert r.result["out_dir"].startswith(str(ws / "exec-out"))
    rw = [c for c in calls[0]["cmd"] if c.startswith("--read-write=")]
    assert len(rw) == 2                     # sandbox workdir + artifact dir
    assert calls[0]["env"]["MPLBACKEND"] == "Agg"


def test_no_work_root_no_artifacts(monkeypatch, exec_ctx):
    monkeypatch.setattr(EX.shutil, "which", lambda name: "/usr/bin/firejail")
    _patch_exec(monkeypatch, _Proc(out=b"hi\n"))
    r = asyncio.run(CodeExecute().execute({"code": "print('hi')"}, exec_ctx))
    assert r.status == "ok"
    assert "out_dir" not in r.result and "written_files" not in r.result
