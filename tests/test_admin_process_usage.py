"""On-demand resource details for the preset actually serving a slot."""

import pytest

from runtime import model_usage


def _canned(pid=4242):
    return {"pid": pid,
            "ram": {"used_gib": 3.5, "processes": 1,
                    "readable_processes": 1, "source": "test"},
            "vram": [{"gpu": "0", "process_gib": 8.0,
                      "card_used_gib": 10.0, "card_total_gib": 16.0}],
            "vram_source": "test-smi",
            "disk": {"artifacts": [], "mounts": [], "total_gib": 4.0,
                     "warnings": []},
            "warnings": [], "collected_at": "now"}


def _mark_running(pm, monkeypatch, name="brain", pid=4242):
    original = pm.status()
    monkeypatch.setattr(pm, "status", lambda: {
        **original, name: {"alive": True, "pid": pid}})


@pytest.mark.asyncio
async def test_running_slot_usage_returns_live_preset(web_app, web_client,
                                                      monkeypatch):
    from runtime import process_manager
    from runtime.preset_store import slot_preset_name
    app = web_app()
    _mark_running(process_manager.CURRENT, monkeypatch)
    monkeypatch.setattr(model_usage, "collect_usage",
                        lambda pid, preset, config: _canned(pid))
    async with web_client(app) as client:
        response = await client.get("/api/admin/processes/brain/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["slot"] == "brain"
    assert body["preset"] == slot_preset_name(app.state.runtime.config, "brain")
    assert body["ram"]["used_gib"] == 3.5
    assert body["vram"][0]["process_gib"] == 8.0
    assert body["swapped"] is False


@pytest.mark.asyncio
async def test_swapped_slot_usage_uses_occupant_preset(web_app, web_client,
                                                       tmp_path, monkeypatch):
    from runtime import serving as S
    from runtime.preset_store import resolve_slot
    app = web_app()
    cfg = app.state.runtime.config
    cfg["models"]["presets"]["dolphin"] = {"preset": "/tmp/dolphin.conf"}
    state_dir = str(tmp_path / "serve")
    cfg["tools"]["serve"]["state_dir"] = state_dir
    port = resolve_slot(cfg, "specialist")["port"]
    S.write_server(state_dir, {
        "name": "dolphin", "model": "dolphin", "pid": 777,
        "port": port, "status": "running"})
    monkeypatch.setattr(S, "pid_alive", lambda pid: True)
    monkeypatch.setattr(model_usage, "collect_usage",
                        lambda pid, preset, config: _canned(pid))
    async with web_client(app) as client:
        response = await client.get("/api/admin/processes/specialist/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["preset"] == "dolphin"
    assert body["pid"] == 777
    assert body["swapped"] is True


@pytest.mark.asyncio
async def test_usage_rejects_stopped_remote_and_unknown_slots(web_app, web_client):
    app = web_app()
    app.state.runtime.config["models"]["presets"][
        app.state.runtime.config["models"]["slots"]["brain"]]["remote_host"] = "gpu-box"
    async with web_client(app) as client:
        stopped = await client.get("/api/admin/processes/specialist/usage")
        remote = await client.get("/api/admin/processes/brain/usage")
        unknown = await client.get("/api/admin/processes/not-a-slot/usage")
    assert stopped.status_code == 409
    assert "no running local preset" in stopped.json()["detail"]
    assert remote.status_code == 409
    assert "remote preset" in remote.json()["detail"]
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_probe_failure_returns_honest_partial_payload(web_app, web_client,
                                                            monkeypatch):
    from runtime import process_manager
    app = web_app()
    _mark_running(process_manager.CURRENT, monkeypatch)
    def fail(pid, preset, config):
        raise OSError("procfs denied")
    monkeypatch.setattr(model_usage, "collect_usage", fail)
    async with web_client(app) as client:
        response = await client.get("/api/admin/processes/brain/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["ram"]["used_gib"] is None
    assert body["vram"] == []
    assert "procfs denied" in body["warnings"][0]
