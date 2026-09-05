"""Admin → Processes ↔ strength-swap lifecycle: a serve-managed occupant
(model.use swap-in) holding a slot's port surfaces as `swap` in the status,
its log is served while the boot process is down, and stop/start/restart
tear the occupant down before the boot preset retakes the slot.

The occupant is a REAL throwaway process (own session, like launch_server's
children) — pid liveness and the stop path act on something genuine."""
import os
import signal
import time

import pytest


def _spawn_sleeper():
    pid = os.fork()
    if pid == 0:
        os.setsid()
        os.execvp("sleep", ["sleep", "600"])
        os._exit(1)
    return pid


@pytest.fixture
def spawned():
    """Reap any sleeper a test planted (pass or fail)."""
    pids = []
    yield pids
    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass


def _plant_occupant(app, tmp_path, spawned, name="dolphin", pid_start=True):
    """A serve-registry entry on the specialist slot's port, backed by a real
    live process — exactly what model.use leaves behind after a swap."""
    from runtime import serving as S
    from runtime.preset_store import resolve_slot
    cfg = app.state.runtime.config
    port = int(resolve_slot(cfg, "specialist")["port"])
    state_dir = str(tmp_path / "serve")
    cfg["tools"]["serve"]["state_dir"] = state_dir
    pid = _spawn_sleeper()
    spawned.append(pid)
    log_dir = tmp_path / "serve" / name
    log_dir.mkdir(parents=True)
    (log_dir / "stderr.log").write_text("dolphin boot line\nserving now\n")
    entry = {"name": name, "kind": "llm", "model": name,
             "served_model_id": "dolphin-llama3.1-8b", "gpu": "1",
             "port": port, "host": "127.0.0.1",
             "base_url": f"http://127.0.0.1:{port}", "pid": pid,
             "command": f"/x/scripts/start-model.sh --preset /x/{name}.conf",
             "log_dir": str(log_dir),
             "started_at": S._now_iso(), "status": "running"}
    if pid_start:
        entry["pid_start"] = S.pid_start_time(pid)
    S.write_server(state_dir, entry)
    return pid


@pytest.mark.asyncio
async def test_swap_occupant_shows_in_status_and_logs(web_app, web_client,
                                                      tmp_path, spawned):
    app = web_app()
    pid = _plant_occupant(app, tmp_path, spawned)
    async with web_client(app) as c:
        r = await c.get("/api/admin/processes")
        assert r.status_code == 200
        spec = r.json()["specialist"]
        assert spec["alive"] is False            # boot process is down…
        swap = spec["swap"]
        assert swap["server"] == "dolphin"       # …but the slot SERVES
        assert swap["model"] == "dolphin-llama3.1-8b"
        assert swap["pid"] == pid

        r = await c.get("/api/admin/processes/specialist/logs")
        assert r.status_code == 200
        body = r.json()
        assert body["swap"] == "dolphin"
        assert "dolphin boot line" in "\n".join(body["lines"])


@pytest.mark.asyncio
async def test_stop_endpoint_tears_down_swap(web_app, web_client,
                                             tmp_path, spawned):
    from runtime import serving as S
    app = web_app()
    pid = _plant_occupant(app, tmp_path, spawned)
    async with web_client(app) as c:
        r = await c.post("/api/admin/processes/specialist/stop")
        assert r.status_code == 200
        assert r.json()["swap_stopped"] == "dolphin"
        deadline = time.time() + 3
        while S.pid_alive(pid) and time.time() < deadline:
            time.sleep(0.1)
        assert not S.pid_alive(pid)
        assert S.read_server(str(tmp_path / "serve"), "dolphin") is None

        r = await c.get("/api/admin/processes")
        assert r.json()["specialist"]["swap"] is None


@pytest.mark.asyncio
async def test_start_frees_slot_then_boots(web_app, web_client,
                                           tmp_path, spawned, monkeypatch):
    from runtime import process_manager
    from runtime import serving as S
    app = web_app()
    pid = _plant_occupant(app, tmp_path, spawned)
    started = []

    async def fake_start_one(name):
        started.append(name)
        return True
    monkeypatch.setattr(process_manager.CURRENT, "start_one", fake_start_one)
    async with web_client(app) as c:
        r = await c.post("/api/admin/processes/specialist/start")
        assert r.status_code == 200
        assert r.json()["swap_stopped"] == "dolphin"
        assert started == ["specialist"]         # occupant gone BEFORE the boot
        assert not S.pid_alive(pid)


@pytest.mark.asyncio
async def test_start_409s_when_occupant_refuses(web_app, web_client,
                                                tmp_path, spawned, monkeypatch):
    """Identity mismatch (no pid_start, no command token): the occupant stays
    and start must NOT boot the preset onto a held port."""
    from runtime import process_manager
    from runtime import serving as S
    app = web_app()
    pid = _plant_occupant(app, tmp_path, spawned, pid_start=False)
    # cmdline is "sleep 600" — strip the path token from the recorded command
    # so the legacy fallback can't match either.
    state_dir = str(tmp_path / "serve")
    e = S.read_server(state_dir, "dolphin")
    e["command"] = "sleep 600"
    S.write_server(state_dir, e)
    started = []

    async def fake_start_one(name):
        started.append(name)
        return True
    monkeypatch.setattr(process_manager.CURRENT, "start_one", fake_start_one)
    async with web_client(app) as c:
        r = await c.post("/api/admin/processes/specialist/start")
        assert r.status_code == 409
        assert "refused to stop" in r.json()["detail"]
        assert started == []
        assert S.pid_alive(pid)                  # untouched — admin intervenes


@pytest.mark.asyncio
async def test_no_occupant_means_no_swap_fields(web_app, web_client, tmp_path):
    app = web_app()
    app.state.runtime.config["tools"]["serve"]["state_dir"] = str(tmp_path / "serve")
    async with web_client(app) as c:
        r = await c.get("/api/admin/processes")
        spec = r.json()["specialist"]
        assert spec["swap"] is None
        r = await c.post("/api/admin/processes/specialist/stop")
        assert r.status_code == 200
        assert r.json()["swap_stopped"] is None
