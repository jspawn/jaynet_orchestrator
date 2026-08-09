"""DB-backed cloud model catalog (runtime/cloud_store.py) + admin routes.

No network: the store runs on tmp sqlite files; the admin routes run through
the in-process web_app/web_client fixtures (conftest points models.presets_db
at a tmp path, so /srv/data is never touched).
"""
import pytest
import yaml

from runtime import cloud_store as cs

LITELLM_SEED = {
    "model_list": [
        {"model_name": "local-orchestrator",
         "litellm_params": {"model": "openai/brain-id",
                            "api_base": "http://127.0.0.1:8090/v1"}},
        {"model_name": "kimi-k3",
         "litellm_params": {"model": "openai/kimi-k3",
                            "api_key": "os.environ/MOONSHOT_API_KEY",
                            "api_base": "https://api.moonshot.ai/v1"}},
        {"model_name": "qwen-plus",
         "litellm_params": {"model": "dashscope/qwen3.6-plus",
                            "api_key": "os.environ/DASHSCOPE_API_KEY",
                            "api_base": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"}},
    ],
    "router_settings": {"fallbacks": [
        {"kimi-k3": ["qwen-plus", "local-orchestrator"]},
        {"qwen-plus": ["local-orchestrator"]}]},
}
COSTS = {"kimi-k3": {"input": 3, "output": 15},
         "qwen-plus": {"input": 0.4, "output": 1.2}}


def _store(tmp_path, seed=True):
    s = cs.CloudStore(str(tmp_path / "p.db"))
    s.ensure(seed={"litellm": LITELLM_SEED, "costs": COSTS} if seed else None)
    return s


def _row(name="nova", alias="nova-1", **kw):
    r = {"name": name, "litellm_alias": alias, "provider_model": "openai/x",
         "api_base": "", "key_env": "NOVA_API_KEY", "input_cost": 1,
         "output_cost": 2, "thinking": "on",
         "fallbacks": ["local-orchestrator"], "enabled": True, "role": "t"}
    r.update(kw)
    return r


# ---- seed + store ------------------------------------------------------------

def test_seed_parses_litellm_yaml(tmp_path):
    rows = _store(tmp_path).list()
    assert [r["name"] for r in rows] == ["kimi", "qwen"]   # _META friendly names
    kimi = rows[0]
    assert kimi["provider_model"] == "openai/kimi-k3"
    assert kimi["key_env"] == "MOONSHOT_API_KEY"           # name only, no value
    assert kimi["input_cost"] == 3 and kimi["output_cost"] == 15
    assert kimi["fallbacks"] == ["qwen-plus", "local-orchestrator"]
    assert rows[1]["thinking"] == "off"                    # qwen default
    assert all("local" not in r["litellm_alias"] for r in rows)


def test_replace_all_validation(tmp_path):
    s = _store(tmp_path)
    s.replace_all([_row()])
    assert s.list()[0]["litellm_alias"] == "nova-1"
    with pytest.raises(ValueError):
        s.replace_all([_row(), _row(alias="nova-2")])          # dup name
    with pytest.raises(ValueError):
        s.replace_all([_row(name="bad name!")])
    with pytest.raises(ValueError):
        s.replace_all([_row(alias="local-orchestrator")])      # reserved
    with pytest.raises(ValueError):
        s.replace_all([_row(provider_model="")])
    with pytest.raises(ValueError):
        s.replace_all([_row(key_env="9BAD")])
    with pytest.raises(ValueError):
        s.replace_all([_row(thinking="maybe")])
    assert s.list()[0]["litellm_alias"] == "nova-1"            # unchanged


def test_layer_into_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    seed_yaml = tmp_path / "litellm.yaml"
    seed_yaml.write_text(yaml.safe_dump(LITELLM_SEED))
    monkeypatch.setenv("ORCH_LITELLM_CONFIG", str(seed_yaml))
    cfg = {"models": {}, "costs": dict(COSTS)}
    assert cs.load_into_config(cfg) is True
    cloud = cfg["models"]["cloud"]
    assert cloud["kimi"]["litellm_alias"] == "kimi-k3"
    assert cloud["qwen"]["thinking"] == "off"
    assert cfg["costs"]["kimi-k3"] == {"input": 3, "output": 15}
    # llm.call's active map was refreshed too
    from tools.llm import cloud_models as cm
    try:
        assert cm.resolve_model_alias("kimi") == "kimi-k3"
        assert "kimi" in cm.CallCloudLLM().parameters["properties"]["model"]["enum"]
    finally:
        cm.set_active({})


def test_render_full_proxy_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("LITELLM_MASTER_KEY", "x")
    s = _store(tmp_path)
    s.replace_all([_row(fallbacks=["qwen-plus", "local-orchestrator"]),
                   _row(name="off", alias="off-1", enabled=False)])
    cfg = {"models": {"presets": {
               "brain": {"served_id": "brain-id", "port": 8090},
               "fable": {"served_id": "fable-id", "port": 8080}},
           "slots": {"brain": "brain", "specialist": "fable"}}}
    doc = yaml.safe_load(cs.render(cfg))
    by_name = {m["model_name"]: m["litellm_params"] for m in doc["model_list"]}
    # locals come from the preset catalog (slot-aware)
    assert by_name["local-orchestrator"]["model"] == "openai/brain-id"
    assert by_name["local-specialist"]["api_base"] == "http://127.0.0.1:8080/v1"
    # enabled cloud row rendered; disabled excluded
    assert by_name["nova-1"]["api_key"] == "os.environ/NOVA_API_KEY"
    assert "off-1" not in by_name
    # fallbacks: filtered to existing aliases, local-specialist always covered
    fb = {k: v for entry in doc["router_settings"]["fallbacks"]
          for k, v in entry.items()}
    assert fb["nova-1"] == ["local-orchestrator"]   # qwen-plus not in catalog
    assert fb["local-specialist"] == ["local-orchestrator"]
    assert doc["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"


def test_render_master_key_optional(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    _store(tmp_path)
    cfg = {"models": {}}
    # unset or empty: localhost-only proxy enforces no auth → key omitted
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    assert "master_key" not in yaml.safe_load(cs.render(cfg))["general_settings"]
    monkeypatch.setenv("LITELLM_MASTER_KEY", "")
    assert "master_key" not in yaml.safe_load(cs.render(cfg))["general_settings"]
    # set: master_key rendered so the proxy enforces it
    monkeypatch.setenv("LITELLM_MASTER_KEY", "x")
    gs = yaml.safe_load(cs.render(cfg))["general_settings"]
    assert gs["master_key"] == "os.environ/LITELLM_MASTER_KEY"


def test_write_rendered_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "db" / "p.db"))
    _store(tmp_path)  # seeds (different path, but ensure creates schema only)
    out = cs.write_rendered({"models": {}}, out=str(tmp_path / "db" / "litellm.yaml"))
    text = open(out).read()
    assert text.startswith("# GENERATED")
    assert yaml.safe_load(text)["model_list"]


def test_cli_render_falls_back_to_seed_copy(tmp_path, monkeypatch, capsys):
    seed_yaml = tmp_path / "seed.yaml"
    seed_yaml.write_text("# seed\nmodel_list: []\n")
    monkeypatch.setenv("ORCH_LITELLM_CONFIG", str(seed_yaml))
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    monkeypatch.setattr(cs, "write_rendered",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert cs._cli_render() == 0                     # never blocks the proxy
    assert open(cs.out_path_for({})).read() == "# seed\nmodel_list: []\n"


# ---- llm.call dynamic catalog -------------------------------------------------

def test_llm_call_dynamic_catalog():
    from tools.llm import cloud_models as cm
    cm.set_active({"nova": {"litellm_alias": "nova-1", "thinking": "off",
                            "role": "test model"}})
    try:
        assert cm.resolve_model_alias("nova") == "nova-1"
        assert cm.resolve_model_alias("Nova_1") == "nova-1"
        assert cm.resolve_model_alias("kimi") is None      # not in catalog
        enum = cm.CallCloudLLM().parameters["properties"]["model"]["enum"]
        assert enum == ["nova"]
        _, off, _ = cm._maps(None)
        assert "nova-1" in off
    finally:
        cm.set_active({})
    assert cm.resolve_model_alias("kimi") == "kimi-k3"     # defaults restored


# ---- admin routes --------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_cloud_models_crud(web_app, web_client, monkeypatch):
    monkeypatch.setenv("TESTKEY_PRESENT", "x")
    app = web_app()
    async with web_client(app) as c:
        d = (await c.get("/api/admin/cloud-models")).json()
        names = [m["name"] for m in d["models"]]
        assert "kimi" in names                    # seeded from repo litellm.yaml
        kimi = next(m for m in d["models"] if m["name"] == "kimi")
        assert kimi["key_env"] == "MOONSHOT_API_KEY"
        assert kimi["key_set"] is False           # not set in the test env

        # replace with a custom catalog
        r = await c.put("/api/admin/cloud-models", json={"models": [
            _row(key_env="TESTKEY_PRESENT"),
            _row(name="cheap", alias="cheap-1", thinking="off", enabled=False)]})
        assert r.status_code == 200, r.text
        j = r.json()
        assert [m["name"] for m in j["models"]] == ["nova", "cheap"]
        assert j["models"][0]["key_set"] is True  # env var present → boolean only
        assert "proxy" in j                       # reload outcome reported
        # layered into runtime config (enabled only) + costs updated
        cfg = app.state.runtime.config
        assert cfg["models"]["cloud"]["nova"]["litellm_alias"] == "nova-1"
        assert "cheap" not in cfg["models"]["cloud"]
        assert cfg["costs"]["nova-1"] == {"input": 1, "output": 2}
        # llm.call's enum follows without a restart
        from tools.llm import cloud_models as cm
        assert "nova" in cm.CallCloudLLM().parameters["properties"]["model"]["enum"]
        # invalid rows rejected, catalog untouched
        assert (await c.put("/api/admin/cloud-models", json={"models": [
            _row(provider_model="")]})).status_code == 400


def test_render_remote_slot_preset(tmp_path, monkeypatch):
    """A slot whose preset has remote_host points the static alias at that
    LAN box instead of loopback; local slots stay on 127.0.0.1."""
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    _store(tmp_path)
    cfg = {"models": {"presets": {
               "brain": {"served_id": "brain-id", "port": 8090,
                         "remote_host": "192.168.1.50"},
               "fable": {"served_id": "fable-id", "port": 8080}},
           "slots": {"brain": "brain", "specialist": "fable"}}}
    doc = yaml.safe_load(cs.render(cfg))
    by_name = {m["model_name"]: m["litellm_params"] for m in doc["model_list"]}
    assert by_name["local-orchestrator"]["api_base"] == \
        "http://192.168.1.50:8090/v1"
    assert by_name["local-specialist"]["api_base"] == "http://127.0.0.1:8080/v1"


def test_render_empty_specialist_falls_back_to_brain(tmp_path, monkeypatch):
    """Specialist slot "" (disabled): the local-specialist alias stays alive,
    pointed at the brain target — same as the down-server fallback."""
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    _store(tmp_path)
    cfg = {"models": {"presets": {
               "brain": {"served_id": "brain-id", "port": 8090},
               "fable": {"served_id": "fable-id", "port": 8080}},
           "slots": {"brain": "brain", "specialist": ""}}}
    doc = yaml.safe_load(cs.render(cfg))
    by_name = {m["model_name"]: m["litellm_params"] for m in doc["model_list"]}
    assert by_name["local-orchestrator"]["api_base"] == "http://127.0.0.1:8090/v1"
    assert by_name["local-specialist"]["api_base"] == "http://127.0.0.1:8090/v1"
    assert by_name["local-specialist"]["model"] == "openai/brain-id"


def test_render_extra_specialists_only_when_assigned(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_PRESETS_DB", str(tmp_path / "p.db"))
    _store(tmp_path)
    cfg = {"models": {"presets": {
               "brain": {"served_id": "brain-id", "port": 8090},
               "fable": {"served_id": "fable-id", "port": 8080},
               "tess": {"served_id": "tess-id", "port": 8081}},
           "slots": {"brain": "brain", "specialist": "fable",
                     "specialist2": "tess", "specialist3": ""}}}
    doc = yaml.safe_load(cs.render(cfg))
    by_name = {m["model_name"]: m["litellm_params"] for m in doc["model_list"]}
    assert by_name["local-specialist2"]["api_base"] == "http://127.0.0.1:8081/v1"
    assert by_name["local-specialist2"]["model"] == "openai/tess-id"
    assert "local-specialist3" not in by_name         # empty slot → no alias
