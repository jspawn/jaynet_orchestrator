"""INTEGRATION: Terminal-Bench-style container eval cases on real podman.

Gated on a podman binary (rootless is fine) — skipped everywhere else. Builds
a tiny image from the locally cached python:3.12-slim base (no network needed
when the base is present) and runs:

1. a full container eval case end-to-end through run_case (lifecycle,
   EVAL_CONTAINER_ID checker, cleanup), and
2. code.execute in container mode against a hand-started container (real
   podman exec, state persistence across calls, host-side artifacts).

Marked clearly: this test starts/stops real containers on the host.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import run

from runtime import eval_runner
from runtime.eval_cases import EvalCase
from runtime.eval_store import EvalStore
from runtime.tool_base import ToolContext
from tools.code.execute import CodeExecute

podman = shutil.which("podman")
pytestmark = pytest.mark.skipif(podman is None,
                                reason="podman not installed — container "
                                       "integration test skipped")

_IMAGE = "benchlab-orch-test-integration:latest"


def _podman(*args, check=True, timeout=300):
    proc = subprocess.run(["podman", *args], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"podman {' '.join(args)} failed: "
                           + proc.stdout.decode("utf-8", "replace")[-500:])
    return proc


@pytest.fixture(scope="module")
def tb_image(tmp_path_factory):
    """Build the throwaway test image once (removed at module teardown)."""
    ctx = tmp_path_factory.mktemp("tb-img")
    (ctx / "Containerfile").write_text(
        "FROM python:3.12-slim\n"
        "RUN mkdir -p /app && echo seeded > /app/seed.txt\n"
        "WORKDIR /app\n", encoding="utf-8")
    _podman("build", "-t", _IMAGE, "-f", str(ctx / "Containerfile"), str(ctx))
    yield _IMAGE
    subprocess.run(["podman", "rmi", "-f", _IMAGE],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---- 1. full container case through run_case -----------------------------------

class _FakeTool:
    def __init__(self, name, gated=False):
        self.name = name
        self.requires_confirmation = gated


class _FakeRegistry:
    def all(self):
        return [_FakeTool("fs.read"), _FakeTool("code.execute"),
                _FakeTool("eval.run")]


class _FakeRuntime:
    def __init__(self):
        self.config = {"eval": {"judge_temperature": 0.0}, "privacy": {},
                       "costs": {}}
        self.registry = _FakeRegistry()
        self.model = "fake-brain"
        self.calls = []

    async def run(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return {"run_id": "fake-1", "status": "ok", "answer": "done",
                "trajectory": "code.execute(x)→ok",
                "tools_used": ["code.execute"],
                "budget": {"iterations": 1, "cost_usd": 0.0,
                           "tokens": {"total": 5}}}


async def _judge_ok(cfg, alias, messages, **kw):
    return {"status": "ok", "model_name": "fake-judge", "cost_usd": 0.0,
            "tokens": 1, "error": None,
            "content": '{"pass": true, "score": 9, "notes": "fine",'
                       ' "classification": "none"}'}


# Grades INSIDE the still-running container: /app must be the mounted case
# work_root, with the image's own seed file and a marker planted host-side.
_CHECKER = '''
import os, pathlib, subprocess, sys
cid = os.environ["EVAL_CONTAINER_ID"]
wd = os.environ["EVAL_CONTAINER_WORKDIR"]
pathlib.Path(os.environ["EVAL_WORK_ROOT"], "marker.txt").write_text("host-side")
proc = subprocess.run(
    ["podman", "exec", "--workdir", wd, cid, "python3", "-c",
     "import os; print(os.getcwd()); print(sorted(os.listdir('.')))"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
out = proc.stdout.decode("utf-8", "replace")
print(out)
ok = (proc.returncode == 0 and out.splitlines()[0].strip() == "/app"
      and "seed.txt" in out and "marker.txt" in out)
sys.exit(0 if ok else 1)
'''


def test_run_case_container_end_to_end(tb_image, tmp_path, monkeypatch):
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime()
    store = EvalStore(tmp_path / "eval.db")
    case = EvalCase(id="ctr-e2e", name="Container smoke", turns=["do it"],
                    expect={"checker": _CHECKER}, judge_rubric="r",
                    container={"image": tb_image, "workdir": "/app"})
    row = run(eval_runner.run_case(rt, case, store))
    assert row["passed"] in (True, 1), row.get("check_failures")
    # code.execute was routed into a real container via tools_patch…
    patch = rt.calls[0][1]["run_overrides"]["tools_patch"]
    cid = patch["code"]["container"]["id"]
    assert cid
    # …and the container is gone afterwards (--rm + stop in finally)
    gone = _podman("container", "exists", cid, check=False)
    assert gone.returncode != 0
    store.close()


def test_run_case_container_skips_unknown_image(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime()
    store = EvalStore(tmp_path / "eval.db")
    case = EvalCase(id="ctr-skip", name="skip", turns=["hi"],
                    judge_rubric="r",
                    container={"image": "benchlab-no-such-image-zzz"})
    row = run(eval_runner.run_case(rt, case, store))
    assert row["skipped"] is True and "bench.import" in row["note"]
    assert rt.calls == []
    store.close()


# ---- 2. code.execute against a hand-started container ---------------------------

def test_code_execute_in_container_real(tb_image, tmp_path):
    """State persists across calls (shared mounted workdir, like a real
    terminal); the image's own /app content is materialized into the work
    root first (the mount would hide it); ORCH_EXEC_OUT artifacts come back
    host-side; the per-call script is cleaned up."""
    work = tmp_path / "work"
    work.mkdir()
    cid, err = eval_runner._container_start(tb_image, "/app", work)
    assert cid, err
    try:
        assert (work / "seed.txt").read_text().strip() == "seeded"
        ctx = ToolContext(request_id="t", budget=None, work_root=str(work),
                          config={"tools": {"code": {
                              "container": {"id": cid, "workdir": "/app"}}}})
        # call 1 writes state; needs_confirmation is False (container = sandbox)
        tool = CodeExecute()
        assert tool.needs_confirmation({"code": "x"}, ctx) is False
        r1 = run(tool.execute({"code": "open('state.txt', 'w').write('42')\n"
                                       "print('wrote')"}, ctx))
        assert r1.status == "ok" and "wrote" in r1.result["stdout"]
        # call 2 sees it — plus the image's seed file; and drops an artifact
        r2 = run(tool.execute(
            {"code": "import os\n"
                     "print(open('state.txt').read())\n"
                     "print(open('seed.txt').read().strip())\n"
                     "open(os.environ['ORCH_EXEC_OUT'] + '/chart.txt', 'w')"
                     ".write('png-ish')"}, ctx))
        assert r2.status == "ok", r2.error
        assert "42" in r2.result["stdout"] and "seeded" in r2.result["stdout"]
        arts = r2.result["written_files"]
        assert len(arts) == 1 and arts[0].endswith("chart.txt")
        assert Path(arts[0]).read_text() == "png-ish"
        # per-call script files never linger in the shared workdir
        assert list(work.glob(".orch-exec-*.py")) == []
    finally:
        _podman("stop", cid, check=False, timeout=30)
