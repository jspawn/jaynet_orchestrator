"""DB-backed preset catalog (runtime/preset_store.py) + admin preset routes.

No network: the store runs on tmp sqlite files; the admin routes run through
the in-process web_app/web_client fixtures (conftest points models.presets_db
at a tmp path, so /srv/data is never touched).
"""
import pytest

from runtime import preset_store as ps


def _seed(tmp_path):
    conf = tmp_path / "brain-src.conf"
    conf.write_text("MODEL_PATH=/srv/models/m.gguf\nCTX_SIZE=4096\n")
    return {"presets": {
        "brain": {"preset": str(conf), "role": "the brain",
                  "alias": "local-orchestrator", "port": 8090, "gpu": "0",
                  "served_id": "m", "vram_gib": 30, "strengths": ["reasoning"]},
        "tess": {"preset": "", "role": "coding", "alias": "local-specialist",
                 "port": 8080, "gpu": "1", "served_id": "tess",
                 "vram_gib": 24, "strengths": ["coding"]},
    }}


def _store(tmp_path, seed=True):
    s = ps.PresetStore(str(tmp_path / "presets.db"))
    s.ensure(seed_models=_seed(tmp_path) if seed else None)
    return s


# ---- store -----------------------------------------------------------------

def test_seed_and_load_roundtrip(tmp_path):
    presets, slots = _store(tmp_path).load()
    assert slots == {"brain": "brain", "specialist2": "", "specialist3": ""}
    b = presets["brain"]
    assert b["alias"] == "local-orchestrator" and b["port"] == 8090
    assert b["strengths"] == ["reasoning"]
    # conf materialized to a real file whose content matches the source
    assert b["preset"].endswith("presets/brain.conf")
    assert "CTX_SIZE=4096" in open(b["preset"]).read()
    # preset without conf text keeps its (empty) source path
    assert presets["tess"]["preset"] == ""


def test_seed_only_once(tmp_path):
    s = _store(tmp_path)
    s.upsert("brain", {"role": "edited"})
    s.ensure(seed_models=_seed(tmp_path))   # must not overwrite
    assert s.get("brain")["role"] == "edited"


def test_seed_relative_preset_path_resolves_against_orch_home(tmp_path,
                                                              monkeypatch):
    home = tmp_path / "home"
    (home / "presets").mkdir(parents=True)
    (home / "presets" / "brain-x.conf").write_text("CTX_SIZE=2048\n")
    monkeypatch.setattr(ps, "HOME", home)   # bound at import; ORCH_HOME in prod
    seed = {"presets": {"brain": {"preset": "presets/brain-x.conf",
                                  "role": "b", "alias": "local-orchestrator",
                                  "served_id": "x"}}}
    s = ps.PresetStore(str(tmp_path / "p.db"))
    s.ensure(seed_models=seed)
    assert "CTX_SIZE=2048" in s.get("brain")["conf"]


def test_upsert_create_update_and_materialize(tmp_path):
    s = _store(tmp_path)
    s.upsert("new1", {"role": "x", "port": "9000", "vram_gib": "12",
                      "strengths": ["a", " b ", ""]},
             conf="CTX_SIZE=1024\n", create=True)
    p = s.get("new1")
    assert p["port"] == 9000 and p["vram_gib"] == 12.0
    assert p["strengths"] == ["a", "b"]
    assert "CTX_SIZE=1024" in open(p["preset"]).read()
    s.upsert("new1", {"role": "y"})
    assert s.get("new1")["role"] == "y"
    assert "CTX_SIZE=1024" in s.get("new1")["conf"]   # conf untouched
    with pytest.raises(ValueError):
        s.upsert("new1", {}, create=True)             # duplicate
    with pytest.raises(ValueError):
        s.upsert("Bad Name!", {}, create=True)        # invalid name
    with pytest.raises(KeyError):
        s.upsert("ghost", {})                         # update non-existent


def test_slots_and_delete_guard(tmp_path):
    s = _store(tmp_path)
    s.set_slot("specialist", "tess")
    assert s.resolve("specialist")["served_id"] == "tess"
    assert s.resolve("brain")["served_id"] == "m"     # slot
    assert s.resolve("tess")["served_id"] == "tess"   # direct preset name
    assert s.resolve("nope") is None
    with pytest.raises(ValueError):
        s.delete("tess")                              # slot-assigned
    with pytest.raises(KeyError):
        s.set_slot("specialist", "ghost")
    s.set_slot("specialist", "brain")                 # unassign tess
    s.delete("tess")                                  # now it deletes
    assert s.get("tess") is None
    with pytest.raises(ValueError):
        s.delete("brain")                             # still serves two slots


def test_load_into_config_layers_and_failsafe(tmp_path, monkeypatch):
    cfg = {"models": _seed(tmp_path)}
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    assert ps.load_into_config(cfg) is True
    assert cfg["models"]["slots"] == {"brain": "brain",
                                      "specialist2": "", "specialist3": ""}
    assert cfg["models"]["presets"]["brain"]["preset"].endswith("brain.conf")
    # fail-safe: bogus db path → False, config keeps YAML presets
    cfg2 = {"models": _seed(tmp_path)}
    monkeypatch.setenv("ORCH_PRESETS_DB", "/proc/1/nope/p.db")
    assert ps.load_into_config(cfg2) is False
    assert cfg2["models"]["presets"]["brain"]["preset"].endswith("brain-src.conf")


def test_resolve_slot_helper():
    cfg = {"models": {"presets": {"hermes": {"alias": "a1"}, "tess": {"alias": "a2"}},
                      "slots": {"brain": "hermes"}}}
    assert ps.resolve_slot(cfg, "brain")["alias"] == "a1"
    assert ps.resolve_slot(cfg, "tess")["alias"] == "a2"   # no slot → name itself
    assert ps.resolve_slot(cfg, "ghost") == {}


def test_cli_resolve(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    monkeypatch.setattr(ps, "_read_yaml_config", lambda: {"models": _seed(tmp_path)})
    assert ps._cli_resolve("brain") == 0
    out = capsys.readouterr().out
    # shlex-quoted: bare values print unquoted, anything unsafe is single-quoted
    assert "_PORT=8090" in out and "_ALIAS=m" in out and "_GPU=0" in out
    assert "_PRESET_FILE=" in out
    assert ps._cli_resolve("ghost") == 0
    assert "not found in preset catalog" in capsys.readouterr().out


# ---- device placement (gpu field) --------------------------------------------

def test_normalize_gpu():
    assert ps.normalize_gpu("") == "" and ps.normalize_gpu(None) == ""
    assert ps.normalize_gpu("cpu") == "" and ps.normalize_gpu("NONE") == ""
    assert ps.normalize_gpu("0") == "0" and ps.normalize_gpu("1") == "1"
    assert ps.normalize_gpu("0,1") == "0,1"
    assert ps.normalize_gpu(" 1, 0 ") == "0,1"      # ordered + deduped
    assert ps.normalize_gpu("1,1") == "1"
    assert ps.normalize_gpu("2,10") == "2,10"       # natural sort, n GPUs
    assert ps.normalize_gpu("9") == "9"             # format ok — existence is
                                                    # the admin route's check
    with pytest.raises(ValueError):
        ps.normalize_gpu("0,,1")
    with pytest.raises(ValueError):
        ps.normalize_gpu("bad id!")


def test_gpu_list():
    assert ps.gpu_list({"gpu": ""}) == []
    assert ps.gpu_list({"gpu": "0"}) == ["0"]
    assert ps.gpu_list({"gpu": "0,1"}) == ["0", "1"]
    assert ps.gpu_list(None) == []


def test_upsert_normalizes_and_validates_device(tmp_path):
    s = _store(tmp_path)
    s.upsert("brain", {"gpu": "1,0"})
    assert s.get("brain")["gpu"] == "0,1"
    s.upsert("brain", {"gpu": "cpu"})
    assert s.get("brain")["gpu"] == ""
    with pytest.raises(ValueError):
        s.upsert("brain", {"gpu": "0,,1"})


def test_seed_tolerates_bad_device(tmp_path):
    seed = _seed(tmp_path)
    seed["presets"]["brain"]["gpu"] = "bad id!"
    s = ps.PresetStore(str(tmp_path / "p.db"))
    s.ensure(seed_models=seed)
    assert s.get("brain")["gpu"] == ""               # falls back to CPU


# ---- GPU topology -------------------------------------------------------------

def test_topology_seed_set_and_layering(tmp_path, monkeypatch):
    seed = _seed(tmp_path)
    seed["gpus"] = ["0", "1", "2"]
    seed["gpu_info"] = {"0": {"label": "big card", "vram_gib": 48}}
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    cfg = {"models": seed}
    assert ps.load_into_config(cfg) is True
    assert cfg["models"]["gpus"] == ["0", "1", "2"]
    assert cfg["models"]["gpu_info"]["0"]["label"] == "big card"

    s = ps.PresetStore(str(tmp_path / "p.db"))
    ids, info = s.get_gpus()
    assert ids == ["0", "1", "2"] and info["0"]["vram_gib"] == 48
    s.set_gpus(["0", "1", "2", "3"], {"3": {"label": "new"}})
    assert s.get_gpus()[0] == ["0", "1", "2", "3"]
    with pytest.raises(ValueError):
        s.set_gpus(["0", "2"])          # "1" still used by the tess preset
    with pytest.raises(ValueError):
        s.set_gpus([])                  # empty topology
    with pytest.raises(ValueError):
        s.set_gpus(["bad id!"])
    # layered into config on the next load
    cfg2 = {"models": _seed(tmp_path)}
    ps.load_into_config(cfg2)
    assert cfg2["models"]["gpus"] == ["0", "1", "2", "3"]


@pytest.mark.asyncio
async def test_admin_gpus_and_device_validation(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        d = (await c.get("/api/admin/presets")).json()
        assert [g["id"] for g in d["gpus"]] == ["0", "1"]   # seeded from YAML

        # add a third GPU with display metadata
        r = await c.put("/api/admin/gpus", json={"gpus": [
            {"id": "0"}, {"id": "1"}, {"id": "2", "label": "big", "vram_gib": 48}]})
        assert r.status_code == 200, r.text
        assert [g["id"] for g in r.json()["gpus"]] == ["0", "1", "2"]
        assert app.state.runtime.config["models"]["gpus"] == ["0", "1", "2"]

        # "all" expands to the full split; unknown ids are rejected
        r = await c.put("/api/admin/presets/brain", json={"gpu": "all"})
        assert r.status_code == 200
        p = next(p for p in r.json()["presets"] if p["name"] == "brain")
        assert p["gpu"] == "0,1,2"
        assert (await c.put("/api/admin/presets/brain",
                            json={"gpu": "5"})).status_code == 400
        # dropping a card a preset uses is refused
        assert (await c.put("/api/admin/gpus", json={
            "gpus": [{"id": "0"}, {"id": "1"}]})).status_code == 409


# ---- binary registry ---------------------------------------------------------

def test_binaries_seed_set_and_layering(tmp_path, monkeypatch):
    seed = _seed(tmp_path)
    seed["binaries"] = {"rocm": {"path": "/x/rocm/llama-server",
                                 "device_env": "HIP_VISIBLE_DEVICES"}}
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    cfg = {"models": seed}
    assert ps.load_into_config(cfg) is True
    assert cfg["models"]["binaries"]["rocm"]["path"] == "/x/rocm/llama-server"

    s = ps.PresetStore(str(tmp_path / "p.db"))
    # rocm unused by presets → replaceable; device_env defaults to HIP
    s.set_binaries({"vulkan": {"path": "/x/vk/llama-server"}})
    assert s.get_binaries()["vulkan"]["device_env"] == "HIP_VISIBLE_DEVICES"
    with pytest.raises(ValueError):
        s.set_binaries({"bad name!": {"path": "/x"}})
    with pytest.raises(ValueError):
        s.set_binaries({"vk": {"path": ""}})
    with pytest.raises(ValueError):
        s.set_binaries({"vk": {"path": "/x", "device_env": "9BAD"}})
    # in-use guard + binary_for
    s.upsert("brain", {"binary": "vulkan"})
    with pytest.raises(ValueError):
        s.set_binaries({})
    assert s.binary_for(s.get("brain")) == ("/x/vk/llama-server",
                                            "HIP_VISIBLE_DEVICES")
    assert s.binary_for(s.get("tess")) == ("", "")      # launcher default
    s.upsert("brain", {"binary": "ghost"})
    with pytest.raises(ValueError):
        s.binary_for(s.get("brain"))


def test_migration_adds_binary_column(tmp_path):
    import sqlite3
    db = str(tmp_path / "old.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE presets(name TEXT PRIMARY KEY, role TEXT, "
              "alias TEXT, port INTEGER, gpu TEXT, served_id TEXT, "
              "vram_gib REAL, strengths TEXT, conf TEXT, source_path TEXT, "
              "updated_at REAL)")
    c.execute("INSERT INTO presets VALUES ('brain',NULL,NULL,8090,'0',NULL,"
              "NULL,'[]','CTX_SIZE=1','src',0)")
    c.commit(); c.close()
    s = ps.PresetStore(db)
    s.ensure()
    assert s.get("brain")["binary"] == ""
    s.upsert("brain", {"binary": "rocm"})
    assert s.get("brain")["binary"] == "rocm"


def test_cli_resolve_binary(tmp_path, monkeypatch, capsys):
    seed = _seed(tmp_path)
    seed["binaries"] = {"vk": {"path": "/x/vk/llama-server",
                               "device_env": "GGML_VK_VISIBLE_DEVICES"}}
    seed["presets"]["tess"]["binary"] = "vk"
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    monkeypatch.setattr(ps, "_read_yaml_config", lambda: {"models": seed})
    assert ps._cli_resolve("tess") == 0
    out = capsys.readouterr().out
    assert "_BIN=/x/vk/llama-server" in out
    assert "_BIN_DEVICE_ENV=GGML_VK_VISIBLE_DEVICES" in out
    assert ps._cli_resolve("brain") == 0            # no binary → launcher default
    assert "_BIN=''" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_admin_binaries_and_preset_binary(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        d = (await c.get("/api/admin/presets")).json()
        assert d["binaries"] == []  # shipped YAML seeds none; admin adds them

        # unknown binary rejected on preset save
        assert (await c.put("/api/admin/presets/brain",
                            json={"binary": "cuda"})).status_code == 400
        # register it, then assign
        r = await c.put("/api/admin/binaries", json={"binaries": [
            {"name": "rocm", "path": "/x/rocm/llama-server",
             "device_env": "HIP_VISIBLE_DEVICES"},
            {"name": "cuda", "path": "/x/cuda/llama-server",
             "device_env": "CUDA_VISIBLE_DEVICES"}]})
        assert r.status_code == 200, r.text
        cfg_bins = app.state.runtime.config["models"]["binaries"]
        assert cfg_bins["cuda"]["path"] == "/x/cuda/llama-server"
        r = await c.put("/api/admin/presets/brain", json={"binary": "cuda"})
        assert r.status_code == 200
        p = next(p for p in r.json()["presets"] if p["name"] == "brain")
        assert p["binary"] == "cuda"
        # dropping an in-use binary → 409
        assert (await c.put("/api/admin/binaries", json={"binaries": [
            {"name": "rocm", "path": "/x/rocm/llama-server"}]})).status_code == 409


# ---- admin routes ----------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_presets_crud_and_slots(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        d = (await c.get("/api/admin/presets")).json()
        names = [p["name"] for p in d["presets"]]
        assert "brain" in names and "specialist" in names   # seeded from YAML
        assert d["slots"]["brain"] == "brain"
        assert all("conf" in p for p in d["presets"])

        # create
        r = await c.post("/api/admin/presets", json={
            "name": "testy", "role": "t", "alias": "local-specialist",
            "port": 8080, "gpu": "1", "served_id": "testy",
            "vram_gib": 10, "strengths": ["coding"], "conf": "CTX_SIZE=512\n"})
        assert r.status_code == 200, r.text
        d = r.json()
        p = next(p for p in d["presets"] if p["name"] == "testy")
        assert p["strengths"] == ["coding"] and "CTX_SIZE=512" in p["conf"]
        # duplicate create → 400
        assert (await c.post("/api/admin/presets",
                             json={"name": "testy"})).status_code == 400

        # update
        r = await c.put("/api/admin/presets/testy", json={"role": "t2",
                                                          "strengths": ["research"]})
        assert r.status_code == 200
        p = next(p for p in r.json()["presets"] if p["name"] == "testy")
        assert p["role"] == "t2" and p["strengths"] == ["research"]
        assert (await c.put("/api/admin/presets/ghost",
                            json={"role": "x"})).status_code == 404

        # slot assignment + delete guard
        r = await c.put("/api/admin/preset-slots",
                        json={"updates": {"specialist": "testy"}})
        assert r.status_code == 200 and r.json()["slots"]["specialist"] == "testy"
        assert app.state.runtime.config["models"]["slots"]["specialist"] == "testy"
        assert (await c.delete("/api/admin/presets/testy")).status_code == 409
        assert (await c.put("/api/admin/preset-slots",
                            json={"updates": {"specialist": "ghost"}})).status_code == 404

        # reassign back, then delete works
        await c.put("/api/admin/preset-slots",
                    json={"updates": {"specialist": "specialist"}})
        r = await c.delete("/api/admin/presets/testy")
        assert r.status_code == 200
        assert "testy" not in [p["name"] for p in r.json()["presets"]]


@pytest.mark.asyncio
async def test_admin_presets_layered_into_runtime_config(web_app, web_client):
    """create_app layered the (tmp) DB catalog over the YAML seed."""
    app = web_app()
    async with web_client(app) as c:
        await c.get("/api/admin/presets")
    models = app.state.runtime.config["models"]
    assert models["slots"]["brain"] == "brain"
    assert models["presets"]["brain"]["preset"].endswith("presets/brain.conf")


# ---- remote (LAN) presets ----------------------------------------------------

def test_remote_seed_and_roundtrip(tmp_path):
    seed = _seed(tmp_path)
    seed["presets"]["attic"] = {"preset": "", "role": "off-box",
                                "alias": "local-attic", "port": 8085,
                                "remote_host": "192.168.1.50",
                                "served_id": "qwen", "strengths": ["bulk"]}
    s = ps.PresetStore(str(tmp_path / "p.db"))
    s.ensure(seed_models=seed)
    p = s.get("attic")
    assert p["remote_host"] == "192.168.1.50" and p["gpu"] == ""
    assert s.load()[0]["attic"]["remote_host"] == "192.168.1.50"


def test_remote_host_validation(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.upsert("brain", {"remote_host": "host:8080"})     # port needs a URL
    with pytest.raises(ValueError):
        s.upsert("brain", {"remote_host": "ftp://x"})       # http(s) only
    with pytest.raises(ValueError):
        s.upsert("brain", {"remote_host": "http://x/v1"})   # no path
    s.upsert("brain", {"remote_host": "LLamabox.LAN"})
    assert s.get("brain")["remote_host"] == "llamabox.lan"  # lowercased
    s.upsert("brain", {"remote_host": "localhost"})
    assert s.get("brain")["remote_host"] == ""              # loopback → local


def test_remote_url_endpoints(tmp_path):
    """Adopted servers (vLLM/Ollama/…): full http(s) URLs, port in the URL."""
    s = _store(tmp_path)
    s.upsert("r", {"remote_host": "HTTP://VLLM-Box:8000/"}, create=True)
    p = s.get("r")
    assert p["remote_host"] == "http://vllm-box:8000"       # normalized
    assert p["port"] == 8000                                # backfilled from URL
    assert ps.remote_base(p) == "http://vllm-box:8000"
    # URL without a port needs no port field (scheme default applies)
    s.upsert("r2", {"remote_host": "https://models.example.com"}, create=True)
    assert ps.remote_base(s.get("r2")) == "https://models.example.com:443"
    # bare host + explicit port still works as before
    assert ps.remote_base({"remote_host": "box.lan", "port": 11434}) \
        == "http://box.lan:11434"


def test_remote_backend_and_caps(tmp_path):
    s = _store(tmp_path)
    s.upsert("r", {"remote_host": "http://ollama-box:11434",
                   "backend": "ollama",
                   "caps": {"vision": False, "thinking": True}},
             create=True)
    p = s.get("r")
    assert p["backend"] == "ollama"
    assert p["caps"] == {"vision": False, "thinking": True}
    # None = "auto": the key is dropped rather than stored
    s.upsert("r", {"caps": {"vision": None, "thinking": True}})
    assert s.get("r")["caps"] == {"thinking": True}
    assert ps.cap(p, "thinking", False) is True
    assert ps.cap(p, "vision", True) is False                # explicit off wins
    with pytest.raises(ValueError, match="invalid backend"):
        s.upsert("r", {"backend": "tgi"})
    with pytest.raises(ValueError, match="unknown capability"):
        s.upsert("r", {"caps": {"tools": True}})
    with pytest.raises(ValueError, match="only meaningful for remote"):
        s.upsert("local1", {"backend": "vllm"}, create=True)  # local = llama launcher
    assert ps.cap({}, "vision", False) is False


def test_remote_requires_port_and_holds_no_gpu(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError, match="need a port"):
        s.upsert("r1", {"remote_host": "192.168.1.50"}, create=True)
    s.upsert("r1", {"remote_host": "192.168.1.50", "port": 8085, "gpu": "0"},
             create=True)
    p = s.get("r1")
    assert p["remote_host"] == "192.168.1.50" and p["gpu"] == ""
    # merged view: clearing the port on a remote preset is refused
    with pytest.raises(ValueError, match="need a port"):
        s.upsert("r1", {"port": None})
    # switching back to local lifts the requirement
    s.upsert("r1", {"remote_host": "", "port": None})
    assert s.get("r1")["remote_host"] == "" and s.get("r1")["port"] is None


def test_migration_adds_remote_host_column(tmp_path):
    import sqlite3
    db = str(tmp_path / "old.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE presets(name TEXT PRIMARY KEY, role TEXT, "
              "alias TEXT, port INTEGER, gpu TEXT, served_id TEXT, "
              "vram_gib REAL, strengths TEXT, binary TEXT, conf TEXT, "
              "source_path TEXT, updated_at REAL)")
    c.execute("INSERT INTO presets VALUES ('brain',NULL,NULL,8090,'0',NULL,"
              "NULL,'[]',NULL,'CTX_SIZE=1','src',0)")
    c.commit(); c.close()
    s = ps.PresetStore(db)
    s.ensure()
    assert s.get("brain")["remote_host"] == ""
    s.upsert("brain", {"remote_host": "192.168.1.50"})
    assert s.get("brain")["remote_host"] == "192.168.1.50"


def test_cli_resolve_refuses_remote(tmp_path, monkeypatch, capsys):
    seed = _seed(tmp_path)
    seed["presets"]["attic"] = {"preset": "", "alias": "local-attic",
                                "port": 8085, "remote_host": "192.168.1.50"}
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    monkeypatch.setattr(ps, "_read_yaml_config", lambda: {"models": seed})
    assert ps._cli_resolve("attic") == 0
    out = capsys.readouterr().out
    assert "REMOTE" in out and "192.168.1.50" in out and "exit 1" in out


@pytest.mark.asyncio
async def test_admin_remote_preset_crud(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        await c.get("/api/admin/presets")
        # remote without a port → 400 from the store's merged check
        r = await c.post("/api/admin/presets",
                         json={"name": "attic", "remote_host": "192.168.1.50"})
        assert r.status_code == 400
        r = await c.post("/api/admin/presets",
                         json={"name": "attic", "alias": "local-attic",
                               "remote_host": "192.168.1.50", "port": 8085,
                               "gpu": "0", "role": "off-box"})
        assert r.status_code == 200
        row = [p for p in r.json()["presets"] if p["name"] == "attic"][0]
        assert row["remote_host"] == "192.168.1.50" and row["gpu"] == ""
        # layered into runtime config like any preset
        models = app.state.runtime.config["models"]
        assert models["presets"]["attic"]["remote_host"] == "192.168.1.50"


# ---- empty (disabled) boot slots + extra specialists --------------------------

def test_set_slot_empty_disables_and_brain_refused(tmp_path):
    s = _store(tmp_path)
    s.set_slot("specialist", "tess")
    s.set_slot("specialist", "")                    # disable
    _, slots = s.load()
    assert slots["specialist"] == ""
    assert s.resolve("specialist") is None          # no like-named fallback
    with pytest.raises(ValueError, match="brain slot may not be empty"):
        s.set_slot("brain", "")
    # re-enable works
    s.set_slot("specialist", "tess")
    assert s.resolve("specialist")["served_id"] == "tess"
    # a disabled slot does not hold a preset against deletion
    s.set_slot("specialist", "")
    s.delete("tess")
    assert s.get("tess") is None


def test_optional_specialist_slots_seed_empty(tmp_path):
    _, slots = _store(tmp_path).load()
    assert slots["specialist2"] == "" and slots["specialist3"] == ""
    # assignable like any slot
    s = _store(tmp_path)
    s.set_slot("specialist2", "tess")
    assert s.resolve("specialist2")["served_id"] == "tess"


def test_cli_resolve_empty_slot_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    monkeypatch.setattr(ps, "_read_yaml_config", lambda: {"models": _seed(tmp_path)})
    assert ps._cli_resolve("specialist2") == 0
    out = capsys.readouterr().out
    assert "empty" in out and "exit 1" in out
    assert "not found in preset catalog" not in out


@pytest.mark.asyncio
async def test_admin_slot_disable_and_process_guards(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        # disable the specialist slot
        r = await c.put("/api/admin/preset-slots",
                        json={"updates": {"specialist": ""}})
        assert r.status_code == 200 and r.json()["slots"]["specialist"] == ""
        # brain may not be disabled
        r = await c.put("/api/admin/preset-slots",
                        json={"updates": {"brain": ""}})
        assert r.status_code == 400
        # layered into runtime config so the process manager sees it
        assert app.state.runtime.config["models"]["slots"]["specialist"] == ""
        # status flags it; manual (re)start is refused with guidance
        procs = (await c.get("/api/admin/processes")).json()
        assert procs["specialist"]["disabled"] is True
        assert procs["brain"]["disabled"] is False
        assert procs["specialist2"]["disabled"] is True   # ships empty
        r = await c.post("/api/admin/processes/specialist/restart")
        assert r.status_code == 409 and "slot is empty" in r.json()["detail"]
        r = await c.post("/api/admin/processes/specialist2/start")
        assert r.status_code == 409
        # re-assign → enabled again
        await c.put("/api/admin/preset-slots",
                    json={"updates": {"specialist": "specialist"}})
        procs = (await c.get("/api/admin/processes")).json()
        assert procs["specialist"]["disabled"] is False


@pytest.mark.asyncio
async def test_admin_remote_slot_process_guards(web_app, web_client):
    """A remote preset in a boot slot: the process manager never launches it
    (audit A1 — it used to burn ~20 restart cycles at every web startup)."""
    app = web_app()
    async with web_client(app) as c:
        r = await c.post("/api/admin/presets",
                         json={"name": "attic", "alias": "local-attic",
                               "remote_host": "192.168.1.50", "port": 8085})
        assert r.status_code == 200
        await c.put("/api/admin/preset-slots",
                    json={"updates": {"specialist": "attic"}})
        procs = (await c.get("/api/admin/processes")).json()
        assert procs["specialist"]["remote"] == "192.168.1.50"
        assert procs["specialist"]["disabled"] is False
        assert procs["brain"]["remote"] == ""
        r = await c.post("/api/admin/processes/specialist/restart")
        assert r.status_code == 409 and "remote slot" in r.json()["detail"]
        r = await c.post("/api/admin/processes/specialist/start")
        assert r.status_code == 409
        r = await c.post("/api/admin/processes/specialist/stop")
        assert r.status_code == 409 and "remote slot" in r.json()["detail"]


def test_remote_url_port_owns_the_port(tmp_path):
    """A URL carrying a port always wins over a conflicting port field —
    runtime prefers the URL, so the stored row must agree with it."""
    s = _store(tmp_path)
    s.upsert("r", {"remote_host": "http://box:9000", "port": 8080}, create=True)
    assert s.get("r")["port"] == 9000
    # a portless URL gets its scheme default written back (row stays complete)
    s.upsert("r2", {"remote_host": "https://models.example.com"}, create=True)
    assert s.get("r2")["port"] == 443
    s.upsert("r3", {"remote_host": "http://box"}, create=True)
    assert s.get("r3")["port"] == 80


def test_remote_host_userinfo_rejected_underscores_allowed(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError, match="no credentials"):
        s.upsert("brain", {"remote_host": "http://user:pass@box:8000"})
    # container-style hostnames with underscores work, bare and as URLs
    s.upsert("r", {"remote_host": "ollama_box", "port": 11434}, create=True)
    assert s.get("r")["remote_host"] == "ollama_box"
    s.upsert("r2", {"remote_host": "http://ollama_box:11434"}, create=True)
    assert s.get("r2")["remote_host"] == "http://ollama_box:11434"


def test_think_switch_aliases():
    base = {"local-orchestrator", "local-specialist", "serve-custom"}
    cfg = {"models": {
        "presets": {
            "brain": {"backend": ""},                                # llama
            "spec": {"backend": "vllm"},
            "spec2": {"backend": "ollama", "caps": {"thinking": True}},
            "specf": {"backend": "", "caps": {"thinking": False}}},
        "slots": {"brain": "brain", "specialist": "spec",
                  "specialist2": "spec2", "specialist3": ""}}}
    ok = ps.think_switch_aliases(cfg, base)
    assert "local-orchestrator" in ok         # llama brain keeps the switch
    assert "local-specialist" not in ok       # adopted vllm without opt-in
    assert "local-specialist2" in ok          # caps.thinking opt-in wins
    assert "local-specialist3" in ok          # empty slot → brain fallback (llama)
    assert "serve-custom" in ok               # non-slot aliases untouched
    # explicit caps.thinking off disables even on a llama backend
    cfg["models"]["slots"]["specialist3"] = "specf"
    cfg["models"]["presets"]["specf"]["served_id"] = "x"
    assert "local-specialist3" not in ps.think_switch_aliases(cfg, base)
