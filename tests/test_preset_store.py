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
    assert slots == {"brain": "brain"}
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
    assert cfg["models"]["slots"] == {"brain": "brain"}
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
    assert '_PORT="8090"' in out and '_ALIAS="m"' in out and '_GPU="0"' in out
    assert '_PRESET_FILE="' in out
    assert ps._cli_resolve("ghost") == 0
    assert "not found in preset catalog" in capsys.readouterr().out


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
