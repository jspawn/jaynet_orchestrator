"""serve.start alias registration and serve.stop's non-blocking stop.

Alias agreement: when the caller passes an explicit `alias` (model.use's dynamic
path passes the catalog preset's), the LiteLLM registration, the stored registry
entry, and the reported/callable alias must all be that alias — not the slugged
server name. serve.stop must run the blocking stop_server (SIGTERM grace loop +
possible SIGKILL) off the event loop, keeping its pid-reuse identity guards.
"""
import asyncio
import json
import time

from runtime.tool_base import ToolContext
from tools.serve import lifecycle as L


def _ctx(state_dir):
    return ToolContext(
        request_id="t",
        config={"tools": {"serve": {"state_dir": str(state_dir)}}},
        budget=None)


def _wire_start(monkeypatch, registered):
    """Fake everything external to ServeStart; capture registrations + writes."""
    monkeypatch.setattr(L.S, "read_server", lambda sd, n: None)
    monkeypatch.setattr(L.S, "taken_ports", lambda sd: set())
    monkeypatch.setattr(L.S, "pick_free_port", lambda base, reserved, host: 8091)
    monkeypatch.setattr(L.S, "gpu_free_gib", lambda ctx, g: 40.0)
    monkeypatch.setattr(L.S, "launch_server", lambda *a, **kw: {
        "pid": 4321, "log_dir": "/l", "gpus": "1",
        "stdout": "/l/stdout.log", "stderr": "/l/stderr.log"})

    async def healthy(base, timeout, pid=None):
        return True

    async def qmi(base):
        return "some-model-gguf"

    async def reg(admin_base, key, alias, base_url, served_id):
        registered.append(alias)
        return True, "mid-1"

    monkeypatch.setattr(L.S, "wait_healthy", healthy)
    monkeypatch.setattr(L.S, "query_model_id", qmi)
    monkeypatch.setattr(L.S, "litellm_register", reg)
    monkeypatch.setattr(L, "_litellm", lambda ctx: ("http://proxy", "k"))
    written = []
    monkeypatch.setattr(L.S, "write_server", lambda sd, e: written.append(dict(e)))
    return written


def test_start_registers_explicit_alias(tmp_path, monkeypatch):
    registered = []
    written = _wire_start(monkeypatch, registered)
    r = asyncio.run(L.ServeStart().execute(
        {"name": "vision", "preset": "/p/vis.conf", "alias": "local-vision"},
        _ctx(tmp_path)))
    assert r.status == "ok"
    # registered == reported == stored == what the caller was told to use
    assert registered == ["local-vision"]
    assert r.result["litellm_alias"] == "local-vision"
    assert "model='local-vision'" in r.result["call_hint"]
    assert written[-1]["litellm_alias"] == "local-vision"


def test_start_defaults_alias_to_server_name(tmp_path, monkeypatch):
    registered = []
    _wire_start(monkeypatch, registered)
    r = asyncio.run(L.ServeStart().execute(
        {"name": "fast llm", "preset": "/p/x.conf"}, _ctx(tmp_path)))
    assert r.status == "ok"
    assert registered == ["fast-llm"]                    # slugged name fallback
    assert r.result["litellm_alias"] == "fast-llm"


def test_stop_runs_blocking_stop_server_off_the_loop(tmp_path, monkeypatch):
    d = tmp_path / "vision"
    d.mkdir()
    (d / "server.json").write_text(json.dumps({
        "name": "vision", "pid": 4321, "log_dir": str(d),
        "litellm_alias": "local-vision", "litellm_model_id": "mid-1"}))

    def slow_stop(entry):          # blocks like the real grace loop would
        time.sleep(0.05)
        return True

    dereg = []

    async def dd(admin_base, key, mid):
        dereg.append(mid)
        return True

    monkeypatch.setattr(L.S, "stop_server", slow_stop)
    monkeypatch.setattr(L.S, "litellm_deregister", dd)
    monkeypatch.setattr(L, "_litellm", lambda ctx: ("http://proxy", "k"))

    async def go():
        t = asyncio.create_task(
            L.ServeStop().execute({"name": "vision"}, _ctx(tmp_path)))
        ticks = 0
        while not t.done():
            await asyncio.sleep(0.01)
            ticks += 1              # only advances if the loop isn't frozen
        return await t, ticks

    r, ticks = asyncio.run(go())
    assert r.status == "ok" and r.result["stopped"] is True
    assert ticks >= 2               # the 0.05s blocking stop ran in a thread
    assert dereg == ["mid-1"]       # alias deregistered
    assert not (d / "server.json").exists()   # registry cleared


def test_start_refuses_remote_preset(tmp_path):
    """serve.start never launches a remote preset — it belongs to another box."""
    ctx = ToolContext(
        request_id="t",
        config={"tools": {"serve": {"state_dir": str(tmp_path)}},
                "models": {"presets": {
                    "attic": {"remote_host": "192.168.1.50", "port": 8085}}}},
        budget=None)
    r = asyncio.run(L.ServeStart().execute(
        {"name": "attic", "preset": "attic"}, ctx))
    assert r.status == "error"
    assert "remote preset" in r.error and "192.168.1.50:8085" in r.error


def test_start_passes_llama_bin_via_env_extra(tmp_path, monkeypatch):
    """model.use resolves the preset registry's binary and passes it as
    llama_bin — it must reach the launch as ENV (launch_server's env_extra),
    not a command prefix: bash's exec does not parse VAR=val after it
    (live: the prefix became the executable path, launch died)."""
    captured = {}

    def launch(sd, name, command, **kw):
        captured.update(kw)
        captured["command"] = command
        return {"pid": 4321, "log_dir": "/l", "gpus": "1",
                "stdout": "/l/stdout.log", "stderr": "/l/stderr.log"}

    monkeypatch.setattr(L.S, "read_server", lambda sd, n: None)
    monkeypatch.setattr(L.S, "taken_ports", lambda sd: set())
    monkeypatch.setattr(L.S, "pick_free_port", lambda base, reserved, host: 8091)
    monkeypatch.setattr(L.S, "gpu_free_gib", lambda ctx, g: 40.0)
    monkeypatch.setattr(L.S, "launch_server", launch)

    async def healthy(base, timeout, pid=None):
        return True

    async def qmi(base):
        return "some-model-gguf"

    monkeypatch.setattr(L.S, "wait_healthy", healthy)
    monkeypatch.setattr(L.S, "query_model_id", qmi)
    monkeypatch.setattr(L.S, "write_server", lambda sd, e: None)
    monkeypatch.setattr(L, "_litellm", lambda ctx: (None, None))

    r = asyncio.run(L.ServeStart().execute(
        {"name": "dolphin", "preset": "/p/dolphin.conf",
         "llama_bin": "/opt/rocm bin/bin/llama-server"}, _ctx(tmp_path)))
    assert r.status == "ok", r.error
    assert captured["env_extra"] == {"LLAMA_BIN": "/opt/rocm bin/bin/llama-server"}
    assert not captured["command"].startswith("LLAMA_BIN")
