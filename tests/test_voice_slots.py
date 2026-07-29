"""Voice slots (runtime/voice_slots.py): a slotted stt preset becomes a managed
whisper process + voice.stt.url; a slotted tts preset rewrites voice.tts.command;
empty slots keep the YAML fallbacks.

Uses a real ProcessManager but never starts anything.
"""
import pytest

from conftest import run
from runtime import voice_slots as vs
from runtime.process_manager import ProcessManager


def _stt_preset(**over):
    p = {"kind": "stt", "binary": "/opt/whisper/bin/whisper-server",
         "port": 8097, "gpu": "",
         "conf": "MODEL_PATH=/srv/models/whisper/ggml-small.bin\n"}
    p.update(over)
    return p


def _tts_preset(**over):
    p = {"kind": "tts", "conf": "COMMAND=piper --model v.onnx --output_file {out}\n"}
    p.update(over)
    return p


def _config(stt=None, tts=None):
    presets, slots = {}, {}
    if stt is not None:
        presets["whisper-small"] = stt
        slots["stt"] = "whisper-small"
    if tts is not None:
        presets["piper-default"] = tts
        slots["tts"] = "piper-default"
    return {"models": {"presets": presets, "slots": slots},
            "voice": {"stt": {"url": "http://yaml-stt:1/inference"},
                      "tts": {"command": "yaml-tts {out}"}}}


# ---- conf parsing -------------------------------------------------------------

def test_parse_conf():
    text = ("# comment line\n"
            "\n"
            "MODEL_PATH=/srv/models/whisper/ggml-small.bin\n"
            "ARGS=--language en  # inline comment\n"
            "SPACED = value with spaces \n"
            "not-a-key-value-line\n")
    assert vs.parse_conf(text) == {
        "MODEL_PATH": "/srv/models/whisper/ggml-small.bin",
        "ARGS": "--language en",
        "SPACED": "value with spaces"}
    assert vs.parse_conf("") == {} and vs.parse_conf(None) == {}


# ---- command building -----------------------------------------------------------

def test_build_whisper_command_cpu():
    cmd = vs.build_whisper_command(_stt_preset())
    assert cmd == ("/opt/whisper/bin/whisper-server "
                   "-m /srv/models/whisper/ggml-small.bin "
                   "--host 127.0.0.1 --port 8097")
    assert "-ngl" not in cmd


def test_build_whisper_command_gpu_adds_ngl():
    cmd = vs.build_whisper_command(_stt_preset(gpu="1"))
    assert cmd.endswith("--port 8097 -ngl 99")


def test_build_whisper_command_args_passthrough():
    cmd = vs.build_whisper_command(
        _stt_preset(conf="MODEL_PATH=/m/ggml-tiny.bin\nARGS=--language en -t 4\n"))
    assert cmd.endswith("--port 8097 --language en -t 4")


def test_build_whisper_command_paths_quoted():
    cmd = vs.build_whisper_command(_stt_preset(
        binary="/opt/my bin/whisper-server",
        conf="MODEL_PATH=/m/my model.bin\n"))
    assert cmd.startswith("'/opt/my bin/whisper-server' -m '/m/my model.bin'")


def test_build_whisper_command_unusable():
    assert vs.build_whisper_command({}) == ""
    assert vs.build_whisper_command(_stt_preset(conf="")) == ""       # no MODEL_PATH
    assert vs.build_whisper_command(_stt_preset(binary="")) == ""     # no binary
    assert vs.build_whisper_command(_stt_preset(port=None)) == ""     # no port


# ---- apply() -------------------------------------------------------------------

def test_apply_registers_whisper_and_wires_config():
    cfg = _config(stt=_stt_preset(), tts=_tts_preset())
    pm = ProcessManager()
    run(vs.apply(cfg, pm))
    assert vs.WHISPER_PROC in pm.names()
    st = pm.status()[vs.WHISPER_PROC]
    assert st["command"] == vs.build_whisper_command(_stt_preset())
    assert st["alive"] is False                     # added, never started
    assert cfg["voice"]["stt"]["url"] == "http://127.0.0.1:8097/inference"
    assert cfg["voice"]["tts"]["command"] == "piper --model v.onnx --output_file {out}"


def test_apply_gpu_env():
    cfg = _config(stt=_stt_preset(gpu="0"))
    pm = ProcessManager()
    run(vs.apply(cfg, pm))
    assert pm._procs[vs.WHISPER_PROC].env == {"HIP_VISIBLE_DEVICES": "0"}
    cfg2 = _config(stt=_stt_preset(gpu=""))
    run(vs.apply(cfg2, ProcessManager()))
    assert cfg2["voice"]["stt"]["url"] == "http://127.0.0.1:8097/inference"


def test_apply_command_change_rebuilds_process():
    cfg = _config(stt=_stt_preset())
    pm = ProcessManager()
    run(vs.apply(cfg, pm))
    before = pm.status()[vs.WHISPER_PROC]["command"]
    cfg["models"]["presets"]["whisper-small"]["port"] = 8098
    run(vs.apply(cfg, pm))
    after = pm.status()[vs.WHISPER_PROC]["command"]
    assert before != after and "--port 8098" in after
    assert cfg["voice"]["stt"]["url"] == "http://127.0.0.1:8098/inference"


def test_apply_unslot_removes_process():
    cfg = _config(stt=_stt_preset())
    pm = ProcessManager()
    run(vs.apply(cfg, pm))
    assert vs.WHISPER_PROC in pm.names()
    del cfg["models"]["slots"]["stt"]               # unslot
    run(vs.apply(cfg, pm))
    assert vs.WHISPER_PROC not in pm.names()


def test_apply_empty_slots_keep_yaml_fallbacks():
    cfg = _config()                                 # nothing slotted
    pm = ProcessManager()
    run(vs.apply(cfg, pm))
    assert pm.names() == []
    assert cfg["voice"]["stt"]["url"] == "http://yaml-stt:1/inference"
    assert cfg["voice"]["tts"]["command"] == "yaml-tts {out}"


def test_apply_unslot_restores_yaml_fallbacks():
    yaml_cfg = {"voice": {"stt": {"url": "http://yaml-stt:1/inference"},
                          "tts": {"command": "yaml-tts {out}"}}}
    cfg = _config(stt=_stt_preset(), tts=_tts_preset())
    pm = ProcessManager()
    run(vs.apply(cfg, pm, yaml_config=yaml_cfg))
    assert cfg["voice"]["stt"]["url"] == "http://127.0.0.1:8097/inference"
    assert cfg["voice"]["tts"]["command"] == "piper --model v.onnx --output_file {out}"
    del cfg["models"]["slots"]["stt"]               # unslot both
    del cfg["models"]["slots"]["tts"]
    run(vs.apply(cfg, pm, yaml_config=yaml_cfg))
    assert vs.WHISPER_PROC not in pm.names()
    assert cfg["voice"]["stt"]["url"] == "http://yaml-stt:1/inference"
    assert cfg["voice"]["tts"]["command"] == "yaml-tts {out}"


def test_apply_wrong_kind_ignored():
    # an llama preset parked on the stt slot (legacy/bypassed validation) → off
    cfg = _config(stt={"kind": "llama", "binary": "rocm", "port": 8090,
                       "conf": "MODEL_PATH=/m.gguf\n"})
    pm = ProcessManager()
    run(vs.apply(cfg, pm))
    assert pm.names() == []
    assert cfg["voice"]["stt"]["url"] == "http://yaml-stt:1/inference"


# ---- admin routes ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_slotting_stt_lists_whisper_process(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        # seeded catalog: whisper-small (stt) + piper-default (tts), tts slotted
        d = (await c.get("/api/admin/presets")).json()
        kinds = {p["name"]: p["kind"] for p in d["presets"]}
        assert kinds["whisper-small"] == "stt" and kinds["piper-default"] == "tts"
        assert "stt" in d["slot_names"] and "tts" in d["slot_names"]
        assert "whisper" not in d["slot_names"]
        assert d["slots"].get("tts") == "piper-default"
        assert "stt" not in d["slots"]

        procs = (await c.get("/api/admin/processes")).json()
        assert "whisper" not in procs

        # slot the stt preset → whisper joins the managed processes (not started:
        # the test app never runs the startup hooks)
        r = await c.put("/api/admin/preset-slots",
                        json={"updates": {"stt": "whisper-small"}})
        assert r.status_code == 200, r.text
        assert r.json()["slots"]["stt"] == "whisper-small"
        procs = (await c.get("/api/admin/processes")).json()
        assert "whisper" in procs
        assert "whisper-server" in procs["whisper"]["command"]
        assert procs["whisper"]["alive"] is False
        cfg = app.state.runtime.config
        assert cfg["voice"]["stt"]["url"] == "http://127.0.0.1:8097/inference"
        assert "piper" in cfg["voice"]["tts"]["command"]

        # unslot again (empty value = off) → process removed, YAML fallback
        # restored live (point the yaml copy at a distinct value first — the
        # seed's url coincidentally matches the slotted port)
        import yaml as _yaml
        cpath = app.state.runtime.config_path
        ycfg = _yaml.safe_load(cpath.open())
        ycfg["voice"]["stt"]["url"] = "http://yaml-fallback:9/inference"
        ycfg["voice"]["tts"]["command"] = "yaml-tts {out}"
        cpath.write_text(_yaml.safe_dump(ycfg))
        r = await c.put("/api/admin/preset-slots", json={"updates": {"stt": ""}})
        assert r.status_code == 200, r.text
        assert "whisper" not in (await c.get("/api/admin/processes")).json()
        assert cfg["voice"]["stt"]["url"] == "http://yaml-fallback:9/inference"
        r = await c.put("/api/admin/preset-slots", json={"updates": {"tts": ""}})
        assert r.status_code == 200, r.text
        assert cfg["voice"]["tts"]["command"] == "yaml-tts {out}"


@pytest.mark.asyncio
async def test_admin_slot_kind_mismatch_409(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.put("/api/admin/preset-slots",
                        json={"updates": {"stt": "brain"}})        # llama on stt
        assert r.status_code == 409 and "stt" in r.json()["detail"]
        r = await c.put("/api/admin/preset-slots",
                        json={"updates": {"brain": "piper-default"}})  # tts on llama
        assert r.status_code == 409
        r = await c.put("/api/admin/preset-slots",
                        json={"updates": {"brain": "whisper-small"}})  # stt on llama
        assert r.status_code == 409
