"""devbox: per-run toolchain containers for code.run — lifecycle, network/
taint rules, cwd mapping, idle reaping, and the code.run wiring (devbox
backend when enabled, classic sandbox with a note when not)."""
import asyncio
import json
import time
from pathlib import Path

import pytest

import tools.code.devbox as D
from runtime.tool_base import ToolContext, ToolResult
from tools.code.run import CodeRun

CFG = {"orchestrator": {"model": "local-orchestrator",
                        "litellm_base": "http://x:4000"},
       "tools": {"code": {"devbox": {"enabled": True}}}}


def _ctx(tmp_path, *, taint=False, cfg=None):
    return ToolContext(request_id="run1234567890abcdef", config=cfg or CFG,
                       budget=None, work_root=str(tmp_path / "work"),
                       tmp_root=str(tmp_path / "tmp"), private_taint=taint)


@pytest.fixture
def podman_calls(monkeypatch, tmp_path):
    """Fake _podman: records argv, serves scripted replies by subcommand.
    Also isolates the devbox state dir — container names are request_id-
    derived, so without this tests leak state into each other."""
    calls = []
    monkeypatch.setattr(D, "_state_dir", lambda ctx: tmp_path / "devbox-state")

    async def fake(*args, timeout=30):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return 0, "[]", ""
        if args[0] == "inspect":
            return 1, "", "no such container"
        if args[0] == "run":
            return 0, "container-id", ""
        if args[0] == "exec":
            return 0, "compiled ok", ""
        if args[0] == "stop":
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(D, "_podman", fake)
    monkeypatch.setattr(D, "_image_ok", None)
    return calls


def test_network_cut_on_private_taint(tmp_path):
    assert D._network(_ctx(tmp_path)) is True            # default: on
    assert D._network(_ctx(tmp_path, taint=True)) is False
    cfg = {"tools": {"code": {"devbox": {"enabled": True, "network": False}}}}
    assert D._network(_ctx(tmp_path, cfg=cfg)) is False


def test_map_cwd(tmp_path):
    ctx = _ctx(tmp_path)
    ctr = {"workdir": "/work", "tmpdir": "/tmp/run"}
    assert D.map_cwd(ctr, tmp_path / "work", ctx) == "/work"
    assert D.map_cwd(ctr, tmp_path / "work" / "src" / "pkg", ctx) == "/work/src/pkg"
    assert D.map_cwd(ctr, tmp_path / "tmp" / "scratch", ctx) == "/tmp/run/scratch"
    with pytest.raises(PermissionError):
        D.map_cwd(ctr, Path("/etc"), ctx)


def test_ensure_reuses_running_container(tmp_path, monkeypatch):
    calls = []

    async def running(*args, timeout=30):
        calls.append(args)
        if args[0] == "inspect":
            return 0, "true\n", ""
        return 0, "", ""
    monkeypatch.setattr(D, "_podman", running)
    monkeypatch.setattr(D, "_image_ok", None)
    ctr = asyncio.run(D.ensure(_ctx(tmp_path)))
    assert ctr is not None and ctr["name"].startswith("jaynet-devbox-")
    assert not any(c[0] == "run" for c in calls)


def test_ensure_starts_container_with_mounts(tmp_path, podman_calls):
    (tmp_path / "work").mkdir()
    ctr = asyncio.run(D.ensure(_ctx(tmp_path)))
    assert ctr is not None and ctr["network"] is True
    run = next(c for c in podman_calls if c[0] == "run")
    argv = " ".join(run)
    assert f"{(tmp_path / 'work').resolve()}:/work:rw" in argv
    assert "--rm" in run and "sleep" in run
    assert "--network" not in run
    # cache volumes shared across runs
    assert "jaynet-devbox-cargo:/usr/local/cargo/registry:rw" in argv


def test_ensure_no_network_when_tainted(tmp_path, podman_calls):
    (tmp_path / "work").mkdir()
    ctr = asyncio.run(D.ensure(_ctx(tmp_path, taint=True)))
    assert ctr["network"] is False
    run = next(c for c in podman_calls if c[0] == "run")
    assert "--network" in run and "none" in run


def test_ensure_none_without_podman(tmp_path, monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda c: None)
    assert asyncio.run(D.ensure(_ctx(tmp_path))) is None


def test_ensure_none_when_image_missing(tmp_path, monkeypatch):
    async def no_image(*args, timeout=30):
        if args[:2] == ("image", "inspect"):
            return 1, "", "no such image"
        return 0, "", ""
    monkeypatch.setattr(D, "_podman", no_image)
    monkeypatch.setattr(D, "_image_ok", None)
    assert asyncio.run(D.ensure(_ctx(tmp_path))) is None


def test_image_probe_reprobes_after_failure(tmp_path, monkeypatch):
    """Enable-before-build order: a failed probe must NOT latch — once the
    image is built, the next call picks it up without a process restart."""
    (tmp_path / "work").mkdir()
    built = {"yes": False}

    async def fake(*args, timeout=30):
        if args[:2] == ("image", "inspect"):
            return (0, "[]", "") if built["yes"] else (1, "", "no such image")
        if args[0] == "inspect":
            return 1, "", "no such container"
        return 0, "", ""
    monkeypatch.setattr(D, "_podman", fake)
    monkeypatch.setattr(D, "_state_dir", lambda ctx: tmp_path / "devbox-state")
    monkeypatch.setattr(D, "_image_ok", None)
    assert asyncio.run(D.ensure(_ctx(tmp_path))) is None      # not built yet
    built["yes"] = True
    assert asyncio.run(D.ensure(_ctx(tmp_path))) is not None  # picked up live


def test_attempt_returns_devbox_result(tmp_path, podman_calls):
    (tmp_path / "work").mkdir()
    r, note = asyncio.run(D.attempt(
        {"command": "cargo build"}, _ctx(tmp_path), tmp_path / "work",
        "cargo build", 120, 200, 12000))
    assert note is None and r is not None and r.status == "ok"
    assert r.result["sandbox"] == "devbox" and r.result["network"] is True
    assert r.result["stdout"] == "compiled ok"
    exe = next(c for c in podman_calls if c[0] == "exec")
    assert "timeout" in exe and "cargo build" in exe


def test_attempt_taint_note(tmp_path, podman_calls):
    (tmp_path / "work").mkdir()
    r, _ = asyncio.run(D.attempt(
        {"command": "cargo build"}, _ctx(tmp_path, taint=True),
        tmp_path / "work", "cargo build", 120, 200, 12000))
    assert r.result["network"] is False
    assert "network: OFF" in r.result["note"]


def test_attempt_none_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda c: None)
    r, note = asyncio.run(D.attempt(
        {"command": "x"}, _ctx(tmp_path), tmp_path, "x", 10, 10, 100))
    assert r is None and "devbox unavailable" in note


def test_reap_idle_stops_only_stale(tmp_path, podman_calls, monkeypatch):
    monkeypatch.setattr(D, "_state_dir", lambda ctx: tmp_path / "devbox")
    sd = tmp_path / "devbox"
    sd.mkdir()
    (sd / "jaynet-devbox-old.json").write_text(json.dumps(
        {"name": "jaynet-devbox-old", "last_use": time.time() - 7200}))
    (sd / "jaynet-devbox-fresh.json").write_text(json.dumps(
        {"name": "jaynet-devbox-fresh", "last_use": time.time()}))
    asyncio.run(D.reap_idle(_ctx(tmp_path)))
    stops = [c for c in podman_calls if c[0] == "stop"]
    assert len(stops) == 1 and "jaynet-devbox-old" in stops[0]
    assert not (sd / "jaynet-devbox-old.json").exists()
    assert (sd / "jaynet-devbox-fresh.json").exists()


# ---- code.run wiring ----

def test_code_run_uses_devbox_when_enabled(project, monkeypatch):
    called = {}

    async def fake_attempt(args, ctx, cwd, command, timeout, ml, mc):
        called["command"] = command
        return ToolResult(status="ok", result={"sandbox": "devbox",
                                               "ok": True}), None
    monkeypatch.setattr(D, "attempt", fake_attempt)
    ctx = ToolContext(request_id="t", config=CFG, budget=None,
                      work_root=str(project))
    r = asyncio.run(CodeRun().execute({"command": "cargo build"}, ctx))
    assert r.status == "ok" and r.tool_name == "code.run"
    assert r.result["sandbox"] == "devbox"
    assert called["command"] == "cargo build"


def test_code_run_devbox_disabled_means_classic_path(project):
    # devbox not enabled → no container involvement at all
    ctx = ToolContext(request_id="t",
                      config={"orchestrator": {"model": "m"},
                              "tools": {"code": {}}},
                      budget=None, work_root=str(project))
    r = asyncio.run(CodeRun().execute({"command": "echo hi"}, ctx))
    assert r.status == "ok" and "hi" in r.result["stdout"]


def test_code_run_devbox_gate_unneeds_confirmation(project):
    # With the devbox enabled and podman on PATH, the container is the
    # sandbox — no confirmation gate even without firejail.
    ctx = ToolContext(request_id="t", config=CFG, budget=None,
                      work_root=str(project))
    import shutil
    if shutil.which("podman") is None:
        pytest.skip("podman not installed on this machine")
    assert CodeRun().needs_confirmation({"command": "x"}, ctx) is False


def test_attempt_cuts_network_on_late_taint(tmp_path, monkeypatch):
    """The container started untainted (network on); a private tool result
    arrived later in the run — the network must be cut live, not just on
    the next container."""
    (tmp_path / "work").mkdir()
    monkeypatch.setattr(D, "_state_dir", lambda ctx: tmp_path / "devbox-state")
    calls = []

    async def fake(*args, timeout=30):
        calls.append(args)
        if args[0] == "inspect":
            return 0, "true\n", ""
        return 0, "", ""
    monkeypatch.setattr(D, "_podman", fake)
    monkeypatch.setattr(D, "_image_ok", None)
    # container started WITHOUT taint → network on (cached in ctr)
    r, _ = asyncio.run(D.attempt(
        {"command": "cargo build"}, _ctx(tmp_path, taint=True),
        tmp_path / "work", "cargo build", 120, 200, 12000))
    disc = [c for c in calls if c[:2] == ("network", "disconnect")]
    assert disc, "late taint did not cut the running container's network"
    assert r.result["network"] is False
