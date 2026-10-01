"""OmniVoice plugin: config merge, file checks, server lifecycle, tools.

No real tts-server — Popen and urlopen are faked at the plugin server
module, and the WAV is a minimal RIFF header.
"""
import asyncio
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

OV_DIR = Path(__file__).resolve().parent.parent / "plugins" / "omnivoice"

WAV_1S = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 64


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, OV_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(tmp_path):
    mod = _load("omnivoice_plugin_server", "server.py")  # the cached name
    for k in ("tts-server", "base.gguf", "codec.gguf"):
        (tmp_path / k).write_bytes(b"x")
    cfg = {"plugins": {"omnivoice": {
        "binary": str(tmp_path / "tts-server"),
        "model": str(tmp_path / "base.gguf"),
        "codec": str(tmp_path / "codec.gguf"),
        "keep_warm_s": 0.05,
        "port": 8798,
    }}}
    yield mod, cfg, tmp_path
    sys.modules.pop("omnivoice_plugin_server", None)
    sys.modules.pop("omnivoice_plugin_tool", None)
    sys.modules.pop("omnivoice_plugin_routes", None)


class _Resp(io.BytesIO):
    def __init__(self, data, ctype="application/json"):
        super().__init__(data)
        self.headers = {"Content-Type": ctype}

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


def _fake_urlopen(speech_payload: bytes = WAV_1S):
    """Health + voices GETs return JSON; speech POST returns the WAV."""
    def _u(req, timeout=None, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/health"):
            return _Resp(b'{"status":"ok"}')
        if url.endswith("/v1/audio/voices"):
            if hasattr(req, "data") and req.data:        # POST = register
                return _Resp(b'{"ok":true}')
            return _Resp(b'{"voices":["freeman","anna"]}')
        if url.endswith("/v1/audio/speech"):
            return _Resp(speech_payload, "audio/wav")
        raise AssertionError(f"unexpected url {url}")
    return _u


def test_settings_defaults():
    mod = _load("omnivoice_plugin_server_t1", "server.py")
    s = mod.settings({})
    assert s["port"] == 8730 and s["language"] == "English"
    s2 = mod.settings({"plugins": {"omnivoice": {"port": "9999",
                                                 "keep_warm_s": 5}}})
    assert s2["port"] == 9999 and s2["keep_warm_s"] == 5.0
    sys.modules.pop("omnivoice_plugin_server_t1", None)


def test_settings_autodiscover_models(tmp_path):
    """Empty model/codec config picks up whatever GGUF pair was dropped
    into the standard model dir — no config edit needed on a quant swap."""
    mod = _load("omnivoice_plugin_server_t1b", "server.py")
    (tmp_path / "omnivoice-base-Q8_0.gguf").write_bytes(b"x")
    (tmp_path / "omnivoice-tokenizer-F32.gguf").write_bytes(b"x")
    mod._MODEL_DIR = str(tmp_path)
    s = mod.settings({})
    assert s["model"].endswith("omnivoice-base-Q8_0.gguf")
    assert s["codec"].endswith("omnivoice-tokenizer-F32.gguf")
    # Explicit config still wins over discovery.
    s2 = mod.settings({"plugins": {"omnivoice": {"model": "/custom/m.gguf"}}})
    assert s2["model"] == "/custom/m.gguf"
    sys.modules.pop("omnivoice_plugin_server_t1b", None)


def test_check_files_missing(server):
    mod, cfg, tmp = server
    bad = {"plugins": {"omnivoice": {"binary": "/nope/x"}}}
    with pytest.raises(mod.OmnivoiceError, match="README"):
        mod.SERVER._check_files(mod.settings(bad))


def test_speak_lifecycle(server, monkeypatch, tmp_path):
    mod, cfg, _ = server
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    spawned = []

    def _popen(cmd, env=None, **kw):
        spawned.append((cmd, env))
        return _FakePopen(cmd, env=env)

    monkeypatch.setattr(mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen())
    fresh = mod.TtsServer()

    async def main():
        out = await fresh.speak(cfg, {"text": "hello world"})
        await asyncio.sleep(0.3)   # let the keep-warm reaper fire
        return out

    out = asyncio.run(main())
    path = Path(out["path"])
    assert path.parent == tmp_path / "audio"
    assert path.read_bytes() == WAV_1S
    cmd, env = spawned[0]
    assert "--model" in cmd and "--codec" in cmd and "--port" in cmd
    # keep_warm_s=0.05 → reaper fired inside the loop: server down again
    assert fresh.proc is None


def test_speak_failure_is_clean_error(server, monkeypatch, tmp_path):
    mod, cfg, _ = server
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakePopen(cmd, env))

    def _boom(req, timeout=None, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/health"):
            return _Resp(b'{"status":"ok"}')
        raise OSError("tts-server exploded")

    monkeypatch.setattr(mod.urllib.request, "urlopen", _boom)
    fresh = mod.TtsServer()
    with pytest.raises(mod.OmnivoiceError, match="speech call failed"):
        asyncio.run(fresh.speak(cfg, {"text": "x"}))


def _ctx(cfg, tmp_path, work=True):
    from runtime.tool_base import ToolContext
    ctx = ToolContext(request_id="r1", config=cfg, budget=None, owner="u1")
    if work:
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        ctx.work_root = str(ws)
    return ctx


def test_tool_contract(server, monkeypatch):
    tool_mod = _load("omnivoice_plugin_tool", "tools/voice.py")
    t = tool_mod.AudioSpeak()
    assert t.name == "audio.speak"
    assert "text" in t.parameters["required"]

    class _Ctx:
        config = {"plugins": {"omnivoice": {"binary": "/nope"}}}

    res = asyncio.run(t.execute({"text": "hi"}, _Ctx()))
    assert res.status == "error" and "README" in res.error
    res2 = asyncio.run(t.execute({"text": "  "}, _Ctx()))
    assert res2.status == "error"


def test_tool_stages_wav_and_mirrors_workspace(server, monkeypatch, tmp_path):
    """Same contract as imagegen: the WAV in DATA/audio is outside the run
    workspace — the tool stages it as a download AND copies it into
    work_root so follow-up tools stay inside the path gate."""
    mod, cfg, _ = server
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakePopen(cmd, env))
    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen())
    tool_mod = _load("omnivoice_plugin_tool", "tools/voice.py")
    t = tool_mod.AudioSpeak()
    events = []
    ctx = _ctx({**cfg, "web": {"outputs_dir": str(tmp_path / "out")}}, tmp_path)

    async def emit(kind, payload):
        events.append((kind, payload))
    ctx.emit = emit

    res = asyncio.run(t.execute({"text": "a cube"}, ctx))
    assert res.status == "ok"
    assert res.result["delivered"]
    assert events and events[0][0] == "output"
    staged = list((tmp_path / "out").rglob("*.wav"))
    assert staged and staged[0].read_bytes() == WAV_1S
    p = Path(res.result["path"])
    assert p.parent == tmp_path / "ws" and p.read_bytes() == WAV_1S
    assert "do NOT call deliver.files" in res.result["note"]


def test_clone_register_and_list(server, monkeypatch, tmp_path):
    mod, cfg, _ = server
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakePopen(cmd, env))
    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen())
    tool_mod = _load("omnivoice_plugin_tool", "tools/voice.py")
    c = tool_mod.AudioClone()
    ctx = _ctx(cfg, tmp_path)
    (tmp_path / "ws" / "ref.wav").write_bytes(WAV_1S)

    r = asyncio.run(c.execute({"action": "register", "name": "anna",
                               "ref_audio": "ref.wav",
                               "ref_text": "exact transcript"}, ctx))
    assert r.status == "ok" and r.result["voice"] == "anna"
    r = asyncio.run(c.execute({"action": "list"}, ctx))
    assert r.status == "ok" and r.result["voices"] == ["anna", "freeman"]


def test_clone_validation(server, monkeypatch, tmp_path):
    tool_mod = _load("omnivoice_plugin_tool", "tools/voice.py")
    c = tool_mod.AudioClone()
    ctx = _ctx({"plugins": {"omnivoice": {}}}, tmp_path)
    (tmp_path / "ws" / "ref.wav").write_bytes(WAV_1S)
    (tmp_path / "ws" / "notaudio.wav").write_bytes(b"not a riff")

    r = asyncio.run(c.execute({"action": "register", "name": "bad name!",
                               "ref_audio": "ref.wav", "ref_text": "t"}, ctx))
    assert r.status == "error" and "letters" in r.error
    r = asyncio.run(c.execute({"action": "register", "name": "ok",
                               "ref_audio": "ref.wav", "ref_text": ""}, ctx))
    assert r.status == "error" and "transcript" in r.error
    r = asyncio.run(c.execute({"action": "register", "name": "ok",
                               "ref_audio": "notaudio.wav", "ref_text": "t"}, ctx))
    assert r.status == "error" and "not a WAV" in r.error
    r = asyncio.run(c.execute({"action": "register", "name": "ok",
                               "ref_audio": "/etc/hostname", "ref_text": "t"}, ctx))
    assert r.status == "error" and "outside your workspace" in r.error


def test_routes_register_appends_shutdown_hook(server):
    """A JayNet stop during a keep-warm window must down tts-server, not
    orphan a GPU-resident process (imagegen pattern, audit #24 D5)."""
    mod, cfg, _ = server
    routes = _load("omnivoice_plugin_routes", "routes.py")
    state = SimpleNamespace(shutdown_hooks=[])
    routes.register(None, state)
    assert len(state.shutdown_hooks) == 1
    fake = _FakePopen(["tts-server"])
    mod.SERVER.proc = fake
    asyncio.run(state.shutdown_hooks[0]())
    assert fake.killed and mod.SERVER.proc is None


def test_keep_warm_reaper_is_tracked(server, monkeypatch, tmp_path):
    """The reaper goes through proc.spawn_background (named, logged,
    cancelled at shutdown) — no bare asyncio.create_task (#23 D7 class)."""
    mod, cfg, _ = server
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakePopen(cmd, env))
    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen())
    import runtime.proc as proc_mod
    real_spawn = proc_mod.spawn_background
    spawned = []

    def tracked(coro, name):
        t = real_spawn(coro, name)
        spawned.append(name)
        return t

    monkeypatch.setattr(proc_mod, "spawn_background", tracked)
    fresh = mod.TtsServer()
    asyncio.run(fresh.speak(cfg, {"text": "a cube", "keep_warm_s": 60}))
    assert spawned == ["omnivoice-keep-warm"]
