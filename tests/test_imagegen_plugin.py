"""Imagegen plugin: config merge, file checks, swap+generate lifecycle.

No real sd-server — Popen and urlopen are faked at the plugin server module,
and the ProcessManager is a stub recording stop/start calls.
"""
import asyncio
import base64
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

IMG_DIR = Path(__file__).resolve().parent.parent / "plugins" / "imagegen"

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, IMG_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(tmp_path, monkeypatch):
    mod = _load("imagegen_plugin_server", "server.py")  # the cached name
    # real files for the config check
    for k in ("sd", "dit.gguf", "te.safetensors", "vae.safetensors"):
        (tmp_path / k).write_bytes(b"x")
    cfg = {"plugins": {"imagegen": {
        "sd_binary": str(tmp_path / "sd"),
        "diffusion_model": str(tmp_path / "dit.gguf"),
        "text_encoder": str(tmp_path / "te.safetensors"),
        "vae": str(tmp_path / "vae.safetensors"),
        "keep_warm_s": 0.05,
        "port": 8799,
    }}}
    yield mod, cfg, tmp_path
    sys.modules.pop("imagegen_plugin_server", None)
    sys.modules.pop("imagegen_plugin_tool", None)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    status = 200


class _FakePopen:
    def __init__(self, cmd, env=None, **kw):
        self.cmd = cmd
        self.env = env
        self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.killed = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class _FakePM:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"specialist": {"alive": True}}

    async def stop_one(self, name):
        self.calls.append(("stop", name))
        return True

    async def start_one(self, name):
        self.calls.append(("start", name))
        return True


def _fake_urlopen(payload):
    def _u(req, timeout=None, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/v1/models"):
            return _Resp(json.dumps({"data": []}).encode())
        return _Resp(json.dumps(payload).encode())
    return _u


def test_settings_defaults():
    mod = _load("imagegen_plugin_server_t1", "server.py")
    s = mod.settings({})
    assert s["port"] == 8720 and s["swap_slot"] == "specialist"
    s2 = mod.settings({"plugins": {"imagegen": {"port": "9999",
                                                "keep_warm_s": 5}}})
    assert s2["port"] == 9999 and s2["keep_warm_s"] == 5.0
    sys.modules.pop("imagegen_plugin_server_t1", None)


def test_check_files_missing(server):
    mod, cfg, tmp = server
    bad = {"plugins": {"imagegen": {"sd_binary": "/nope/x"}}}
    with pytest.raises(mod.ImagegenError, match="README"):
        mod.SERVER._check_files(mod.settings(bad))


def test_generate_swaps_and_restores(server, monkeypatch, tmp_path):
    mod, cfg, _ = server
    pm = _FakePM()
    import runtime.process_manager as procm
    monkeypatch.setattr(procm, "CURRENT", pm)
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    spawned = []

    def _popen(cmd, env=None, **kw):
        spawned.append((cmd, env))
        return _FakePopen(cmd, env=env)

    monkeypatch.setattr(mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        _fake_urlopen({"data": [{"b64_json": base64.b64encode(
                            PNG_1PX).decode()}]}))
    fresh = mod.SdServer()

    async def main():
        out = await fresh.generate(cfg, {"prompt": "a red cube"})
        await asyncio.sleep(0.3)   # let the keep-warm reaper fire
        return out

    out = asyncio.run(main())
    path = Path(out["path"])
    assert path.read_bytes() == PNG_1PX
    cmd, env = spawned[0]
    assert "--diffusion-model" in cmd and "--llm" in cmd and "--vae" in cmd
    assert ("stop", "specialist") in pm.calls
    # keep_warm_s=0 → immediate reaper ran inside the loop: slot restored
    assert ("start", "specialist") in pm.calls


def test_generate_failure_wakes_slot(server, monkeypatch, tmp_path):
    mod, cfg, _ = server
    pm = _FakePM()
    import runtime.process_manager as procm
    monkeypatch.setattr(procm, "CURRENT", pm)
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakePopen(cmd, env))

    def _boom(req, timeout=None, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/v1/models"):
            return _Resp(json.dumps({"data": []}).encode())
        raise OSError("sd-server exploded")

    monkeypatch.setattr(mod.urllib.request, "urlopen", _boom)
    fresh = mod.SdServer()
    with pytest.raises(mod.ImagegenError, match="generation call failed"):
        asyncio.run(fresh.generate(cfg, {"prompt": "x"}))
    assert pm.calls == [("stop", "specialist"), ("start", "specialist")]


def test_tool_contract(server, monkeypatch):
    tool_mod = _load("imagegen_plugin_tool", "tools/image.py")
    t = tool_mod.ImageGenerate()
    assert t.name == "image.generate"
    assert "prompt" in t.parameters["required"]

    class _Ctx:
        config = {"plugins": {"imagegen": {"sd_binary": "/nope"}}}

    res = asyncio.run(t.execute({"prompt": "a cube"}, _Ctx()))
    assert res.status == "error" and "README" in res.error
    res2 = asyncio.run(t.execute({"prompt": "  "}, _Ctx()))
    assert res2.status == "error"


def test_tool_stages_png_as_download(server, monkeypatch, tmp_path):
    """The PNG lands in DATA/images — outside the run workspace — so the
    tool itself stages it as a user download; a brain-side deliver.files
    on that path is refused (live: first smoke generation errored there)."""
    mod, cfg, _ = server
    pm = _FakePM()
    import runtime.process_manager as procm
    monkeypatch.setattr(procm, "CURRENT", pm)
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakePopen(cmd, env))
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        _fake_urlopen({"data": [{"b64_json": base64.b64encode(
                            PNG_1PX).decode()}]}))
    tool_mod = _load("imagegen_plugin_tool", "tools/image.py")
    t = tool_mod.ImageGenerate()
    events = []

    class _Ctx:
        config = {**cfg, "web": {"outputs_dir": str(tmp_path / "out")}}
        request_id = "r1"
        owner = "u1"

        async def emit(self, kind, payload):
            events.append((kind, payload))

    res = asyncio.run(t.execute({"prompt": "a cube"}, _Ctx()))
    assert res.status == "ok"
    assert res.result["delivered"]
    assert events and events[0][0] == "output"
    staged = list((tmp_path / "out").rglob("*.png"))
    assert staged and staged[0].read_bytes() == PNG_1PX
