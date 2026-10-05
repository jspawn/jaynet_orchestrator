"""Imagegen plugin: config merge, file checks, swap+generate lifecycle.

No real sd-server — Popen and urlopen are faked at the plugin server module,
and the ProcessManager is a stub recording stop/start calls.
"""
import asyncio
import base64
import importlib.util
import io
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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
    def __init__(self, slots=("specialist",), commands=None):
        self.calls = []
        self._slots = {s: {"alive": True,
                           "command": (commands or {}).get(s, "")}
                       for s in slots}

    def status(self):
        return self._slots

    async def stop_one(self, name):
        self.calls.append(("stop", name))
        self._slots[name]["alive"] = False
        return True

    async def start_one(self, name):
        self.calls.append(("start", name))
        self._slots[name]["alive"] = True
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
    work = tmp_path / "ws"
    work.mkdir()

    class _Ctx:
        config = {**cfg, "web": {"outputs_dir": str(tmp_path / "out")}}
        request_id = "r1"
        owner = "u1"
        work_root = str(work)

        async def emit(self, kind, payload):
            events.append((kind, payload))

    res = asyncio.run(t.execute({"prompt": "a cube"}, _Ctx()))
    assert res.status == "ok"
    assert res.result["delivered"]
    assert events and events[0][0] == "output"
    staged = list((tmp_path / "out").rglob("*.png"))
    assert staged and staged[0].read_bytes() == PNG_1PX
    # The DATA/images original is dropped once staged AND mirrored — the
    # download bundle and the workspace copy are the artifacts, the dir
    # must not grow forever.
    assert not list((tmp_path / "images").glob("*.png"))


def test_tool_mirrors_png_into_workspace(server, monkeypatch, tmp_path):
    """The server-side PNG lands in DATA/images — outside the run workspace,
    so the tool copies it into work_root and returns that path — follow-up
    tools (fs.*, llm.call vision, deliver.files) stay inside the path gate."""
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
    work = tmp_path / "ws"
    work.mkdir()

    class _Ctx:
        config = {**cfg, "web": {"outputs_dir": str(tmp_path / "out")}}
        request_id = "r1"
        owner = "u1"
        work_root = str(work)

        async def emit(self, kind, payload):
            pass

    res = asyncio.run(t.execute({"prompt": "a cube"}, _Ctx()))
    assert res.status == "ok"
    p = Path(res.result["path"])
    assert p.parent == work and p.read_bytes() == PNG_1PX
    assert "do NOT call deliver.files" in res.result["note"]


def test_routes_register_appends_shutdown_hook(server):
    """Audit #24 D5: a JayNet stop during a keep-warm window must down
    sd-server, not orphan a GPU-resident process."""
    mod, cfg, _ = server
    routes = _load("imagegen_plugin_routes", "routes.py")
    state = SimpleNamespace(shutdown_hooks=[])
    routes.register(None, state)
    assert len(state.shutdown_hooks) == 1
    fake = _FakePopen(["sd"])
    mod.SERVER.proc = fake
    asyncio.run(state.shutdown_hooks[0]())
    assert fake.killed and mod.SERVER.proc is None
    sys.modules.pop("imagegen_plugin_routes", None)


def test_keep_warm_reaper_is_tracked(server, monkeypatch, tmp_path):
    """The reaper goes through proc.spawn_background (named, logged,
    cancelled at shutdown) — no bare asyncio.create_task (#23 D7 class)."""
    mod, cfg, _ = server
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakePopen(cmd, env))
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        _fake_urlopen({"data": [{"b64_json": base64.b64encode(
                            PNG_1PX).decode()}]}))
    import runtime.proc as proc_mod
    real_spawn = proc_mod.spawn_background
    spawned = []

    def tracked(coro, name):
        t = real_spawn(coro, name)
        spawned.append(name)
        return t

    monkeypatch.setattr(proc_mod, "spawn_background", tracked)
    fresh = mod.SdServer()
    asyncio.run(fresh.generate(cfg, {"prompt": "a cube", "keep_warm_s": 60}))
    assert spawned == ["imagegen-keep-warm"]


def test_generate_cancel_restores_slot_and_arms_reaper(server, monkeypatch,
                                                       tmp_path):
    """Audit #27 C2: CancelledError is BaseException — it bypassed
    `except Exception`, so a cancel mid-POST leaked the hibernated slot AND
    left the keep-warm reaper disarmed (sd-server stranded on the GPU).
    Multi-slot: EVERY stopped slot comes back, in reverse stop order."""
    mod, cfg, _ = server
    cfg = {"plugins": {"imagegen": {**cfg["plugins"]["imagegen"],
                                    "swap_slots": ["specialist", "brain"]}}}
    pm = _FakePM(("specialist", "brain"))
    import runtime.process_manager as procm
    monkeypatch.setattr(procm, "CURRENT", pm)
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakePopen(cmd, env))

    def _cancel(req, timeout=None, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/v1/models"):
            return _Resp(json.dumps({"data": []}).encode())
        raise asyncio.CancelledError()

    monkeypatch.setattr(mod.urllib.request, "urlopen", _cancel)
    fresh = mod.SdServer()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fresh.generate(cfg, {"prompt": "x", "keep_warm_s": 60}))
    assert pm.calls == [("stop", "specialist"), ("stop", "brain"),
                        ("start", "brain"), ("start", "specialist")]
    assert fresh._reaper is not None


def test_settings_swap_slots_merge():
    """swap_slots (list) + swap_slot (single, back-compat) merge into one
    ordered unique list: swap_slots first, then swap_slot if new; neither
    set → the default ["specialist"]."""
    mod = _load("imagegen_plugin_server_t2", "server.py")
    s = mod.settings({})
    assert s["swap_slot"] == "specialist"
    assert s["swap_slots"] == ["specialist"]
    s = mod.settings({"plugins": {"imagegen": {
        "swap_slots": ["brain", "specialist"], "swap_slot": "specialist"}}})
    assert s["swap_slots"] == ["brain", "specialist"]
    s = mod.settings({"plugins": {"imagegen": {
        "swap_slots": ["brain"], "swap_slot": ""}}})
    assert s["swap_slots"] == ["brain"]
    s = mod.settings({"plugins": {"imagegen": {"swap_slot": "vision"}}})
    assert s["swap_slots"] == ["vision"]
    s = mod.settings({"plugins": {"imagegen": {
        "swap_slots": ["specialist"]}}})
    assert s["swap_slots"] == ["specialist"]   # default swap_slot deduped
    s = mod.settings({"plugins": {"imagegen": {
        "restore_ready_timeout_s": 5}}})
    assert s["restore_ready_timeout_s"] == 5.0
    sys.modules.pop("imagegen_plugin_server_t2", None)


def test_generate_multi_slot_stop_and_reverse_restore(server, monkeypatch,
                                                      tmp_path):
    """swap_slots hibernates every alive slot in list order and restores
    them in REVERSE order (LIFO): the brain comes back right after the
    generation POST, the specialist when the keep-warm reaper fires."""
    mod, cfg, _ = server
    cfg = {"plugins": {"imagegen": {**cfg["plugins"]["imagegen"],
                                    "swap_slots": ["specialist", "brain"]}}}
    pm = _FakePM(("specialist", "brain"))
    import runtime.process_manager as procm
    monkeypatch.setattr(procm, "CURRENT", pm)
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakePopen(cmd, env))
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        _fake_urlopen({"data": [{"b64_json": base64.b64encode(
                            PNG_1PX).decode()}]}))
    fresh = mod.SdServer()

    async def main():
        out = await fresh.generate(cfg, {"prompt": "a red cube"})
        await asyncio.sleep(0.3)   # let the keep-warm reaper fire
        return out

    out = asyncio.run(main())
    assert out["slots_hibernated"] == ["specialist", "brain"]
    assert out["slot_hibernated"] == "specialist"   # back-compat key
    assert out["restore_not_ready"] == []
    assert pm.calls == [("stop", "specialist"), ("stop", "brain"),
                        ("start", "brain"), ("start", "specialist")]


def test_brain_restore_waits_for_ready(server, monkeypatch, tmp_path):
    """The parent run's very next turn calls the brain — a hibernated brain
    is restarted right after the generation POST and the result only
    returns once the brain's server answers (port from the preset store)."""
    mod, cfg, _ = server
    cfg = {"plugins": {"imagegen": {**cfg["plugins"]["imagegen"],
                                    "swap_slots": ["specialist", "brain"],
                                    "keep_warm_s": 60}},
           "models": {"slots": {"brain": "brain-x"},
                      "presets": {"brain-x": {"port": 8093}}}}
    pm = _FakePM(("specialist", "brain"))
    import runtime.process_manager as procm
    monkeypatch.setattr(procm, "CURRENT", pm)
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakePopen(cmd, env))
    probes = []

    def _u(req, timeout=None, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url == "http://127.0.0.1:8093/v1/models":
            probes.append(url)
            return _Resp(json.dumps({"data": []}).encode())
        if url.endswith("/v1/models"):
            return _Resp(json.dumps({"data": []}).encode())
        return _Resp(json.dumps({"data": [{"b64_json": base64.b64encode(
            PNG_1PX).decode()}]}).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", _u)
    fresh = mod.SdServer()
    out = asyncio.run(fresh.generate(cfg, {"prompt": "x"}))
    assert probes == ["http://127.0.0.1:8093/v1/models"]   # the ready wait
    assert out["restore_not_ready"] == []
    # brain back immediately; the specialist waits for the keep-warm reaper
    assert pm.calls == [("stop", "specialist"), ("stop", "brain"),
                        ("start", "brain")]


def test_brain_restore_timeout_logs_and_reports(server, monkeypatch,
                                                tmp_path, caplog):
    """A restored slot that never answers in time must not hang the tool:
    bounded wait, loud log, and the miss reported in the result (the run's
    model-error retry can still recover). Port parsed from the launch
    command here (--port N) — the preset-store fallback."""
    mod, cfg, _ = server
    cfg = {"plugins": {"imagegen": {**cfg["plugins"]["imagegen"],
                                    "swap_slots": ["specialist", "brain"],
                                    "keep_warm_s": 60,
                                    "restore_ready_timeout_s": 0.05}}}
    pm = _FakePM(("specialist", "brain"),
                 commands={"brain": "llama-server --port 8094 -m b.gguf"})
    import runtime.process_manager as procm
    monkeypatch.setattr(procm, "CURRENT", pm)
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakePopen(cmd, env))

    def _u(req, timeout=None, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url == "http://127.0.0.1:8094/v1/models":
            raise OSError("brain still loading")
        if url.endswith("/v1/models"):
            return _Resp(json.dumps({"data": []}).encode())
        return _Resp(json.dumps({"data": [{"b64_json": base64.b64encode(
            PNG_1PX).decode()}]}).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", _u)
    fresh = mod.SdServer()
    with caplog.at_level(logging.WARNING, logger="imagegen"):
        out = asyncio.run(fresh.generate(cfg, {"prompt": "x"}))
    assert out["restore_not_ready"] == ["brain"]
    assert pm.calls == [("stop", "specialist"), ("stop", "brain"),
                        ("start", "brain")]
    assert any("not answering" in r.getMessage() and "8094" in r.getMessage()
               for r in caplog.records)


def test_generate_names_unique_same_second(server, monkeypatch, tmp_path):
    """Audit #27 D6: second-resolution + pid names collided for two calls
    in the same second from the same process — a uuid suffix keeps them
    distinct."""
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
    monkeypatch.setattr(mod.time, "strftime", lambda fmt: "20261005-120000")
    fresh = mod.SdServer()

    async def main():
        a = await fresh.generate(cfg, {"prompt": "one"})
        b = await fresh.generate(cfg, {"prompt": "two"})
        return a, b

    a, b = asyncio.run(main())
    assert a["path"] != b["path"]
    assert Path(a["path"]).read_bytes() == PNG_1PX
    assert Path(b["path"]).read_bytes() == PNG_1PX


def test_tool_mirror_overwrites_stale_copy(server, monkeypatch, tmp_path):
    """Audit #27 D6: the workspace mirror skipped same-named files — a
    regenerated artifact silently kept the old bytes. A content mismatch
    must re-copy."""
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
    monkeypatch.setattr(mod.time, "strftime", lambda fmt: "20261005-120000")
    monkeypatch.setattr(mod.uuid, "uuid4",
                        lambda: SimpleNamespace(hex="deadbeefcafe"))
    tool_mod = _load("imagegen_plugin_tool", "tools/image.py")
    t = tool_mod.ImageGenerate()
    work = tmp_path / "ws"
    work.mkdir()
    name = f"20261005-120000-{os.getpid()}-deadbeef.png"
    (work / name).write_bytes(b"stale")

    class _Ctx:
        config = {**cfg, "web": {"outputs_dir": str(tmp_path / "out")}}
        request_id = "r1"
        owner = "u1"
        work_root = str(work)

        async def emit(self, kind, payload):
            pass

    res = asyncio.run(t.execute({"prompt": "a cube"}, _Ctx()))
    assert res.status == "ok"
    assert (work / name).read_bytes() == PNG_1PX
    assert Path(res.result["path"]).read_bytes() == PNG_1PX


def test_tool_mirror_failure_keeps_original_path(server, monkeypatch,
                                                 tmp_path):
    """Audit #27 D7: a failed mirror + successful staging used to return the
    DATA/images path AFTER unlinking it — a deleted path. The original is
    only dropped once BOTH the bundle and the mirror hold a copy."""
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
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a dir")   # copyfile into it fails

    class _Ctx:
        config = {**cfg, "web": {"outputs_dir": str(tmp_path / "out")}}
        request_id = "r1"
        owner = "u1"
        work_root = str(blocked)

        async def emit(self, kind, payload):
            pass

    res = asyncio.run(t.execute({"prompt": "a cube"}, _Ctx()))
    assert res.status == "ok"
    assert res.result["delivered"]
    p = Path(res.result["path"])
    assert p.exists() and p.read_bytes() == PNG_1PX
    assert p.parent == tmp_path / "images"
