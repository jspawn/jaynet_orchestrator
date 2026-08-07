"""Audit fixes: config path portability (B3) + keyless LiteLLM mode (B8).

B3: runtime/config_loader.resolve_paths anchors the allowlisted relative
paths of a parsed runtime.yaml to ORCH_HOME (install tree) or ORCH_DATA
(runtime state); absolute paths pass through untouched.
B8: runtime/model_client._auth_headers sends no Authorization header when
LITELLM_MASTER_KEY is unset (keyless localhost proxy), and a Bearer header
when it is set.
"""
from pathlib import Path

import yaml

from runtime import paths
from runtime.config_loader import load_config, resolve_paths
from runtime.model_client import ModelClientMixin

ROOT = Path(__file__).resolve().parent.parent


def _cfg(**over):
    base = {
        "skills": {"dir": "skills"},
        "trace": {"db_path": "trace.db"},
        "web": {"chats_db": "chats.db", "uploads_dir": "uploads"},
        "tools": {"code": {"python": ".venv/bin/python",
                           "workdir": "code-sandbox"}},
        "processes": {"brain": {"command": "scripts/start-model.sh brain"}},
    }
    base.update(over)
    return base


def _roots(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    monkeypatch.setattr(paths, "HOME", home)   # bound at import in prod
    monkeypatch.setattr(paths, "DATA", data)
    return home, data


# ---- B3: relative path resolution ---------------------------------------------

def test_relative_paths_resolve_against_orch_roots(tmp_path, monkeypatch):
    home, data = _roots(tmp_path, monkeypatch)
    cfg = resolve_paths(_cfg())
    assert cfg["skills"]["dir"] == str(home / "skills")
    assert cfg["trace"]["db_path"] == str(data / "trace.db")
    assert cfg["web"]["chats_db"] == str(data / "chats.db")
    assert cfg["web"]["uploads_dir"] == str(data / "uploads")
    assert cfg["tools"]["code"]["python"] == str(home / ".venv/bin/python")
    assert cfg["tools"]["code"]["workdir"] == str(data / "code-sandbox")
    assert (cfg["processes"]["brain"]["command"]
            == f"{home}/scripts/start-model.sh brain")


def test_absolute_paths_pass_through_untouched(tmp_path, monkeypatch):
    _roots(tmp_path, monkeypatch)
    cfg = resolve_paths(_cfg(
        skills={"dir": "/opt/skills"},
        trace={"db_path": "/var/lib/jaynet/trace.db"},
        processes={"brain": {"command": "/usr/local/bin/start.sh brain"}}))
    assert cfg["skills"]["dir"] == "/opt/skills"
    assert cfg["trace"]["db_path"] == "/var/lib/jaynet/trace.db"
    assert cfg["processes"]["brain"]["command"] == "/usr/local/bin/start.sh brain"


def test_non_path_values_and_missing_keys_untouched(tmp_path, monkeypatch):
    _roots(tmp_path, monkeypatch)
    assert resolve_paths(None) is None
    assert resolve_paths([]) == []
    assert resolve_paths({}) == {}
    cfg = resolve_paths({
        "skills": {},                                   # key absent
        "trace": {"db_path": None},                     # null stays null
        "processes": {"x": {"command": "python -m app"},  # PATH lookup
                      "y": "not-a-dict",
                      "z": {"restart": True}},
    })
    assert cfg["skills"] == {}
    assert cfg["trace"]["db_path"] is None
    assert cfg["processes"]["x"]["command"] == "python -m app"


def test_load_config_end_to_end(tmp_path, monkeypatch):
    home, data = _roots(tmp_path, monkeypatch)
    p = tmp_path / "runtime.yaml"
    p.write_text(yaml.safe_dump({
        "models": {"presets_db": "presets.db"},
        "tools": {"serve": {"dispatcher": "scripts/start-model.sh",
                            "state_dir": "serve"},
                  "test": {"project_root": "."}},
    }))
    cfg = load_config(p)
    assert cfg["models"]["presets_db"] == str(data / "presets.db")
    assert (cfg["tools"]["serve"]["dispatcher"]
            == str(home / "scripts/start-model.sh"))
    assert cfg["tools"]["serve"]["state_dir"] == str(data / "serve")
    assert cfg["tools"]["test"]["project_root"] == str(home)


def test_shipped_config_resolves_to_absolute_paths():
    """The shipped runtime.yaml is relative — after load_config every
    allowlisted key it sets must be absolute and anchored at the roots."""
    cfg = load_config(ROOT / "config" / "runtime.yaml")
    home, data = paths.HOME, paths.DATA
    assert cfg["skills"]["dir"] == str(home / "skills")
    assert cfg["chains"]["dir"] == str(home / "chains")
    assert cfg["models"]["presets_db"] == str(data / "presets.db")
    assert cfg["trace"]["db_path"] == str(data / "trace.db")
    web = cfg["web"]
    for key, rel in (("chats_db", "chats.db"), ("users_db", "users.db"),
                     ("uploads_dir", "uploads"), ("outputs_dir", "outputs"),
                     ("projects_dir", "projects"),
                     ("chat_scratch_dir", "chat-scratch")):
        assert web[key] == str(data / rel), key
    tools = cfg["tools"]
    assert tools["ops"]["venv_bin"] == str(home / ".venv/bin")
    assert tools["ops"]["project_root"] == str(home)
    assert tools["code"]["python"] == str(home / ".venv/bin/python")
    assert tools["code"]["workdir"] == str(data / "code-sandbox")
    assert tools["schedule"]["store"] == str(data / "schedules.json")
    assert tools["serve"]["state_dir"] == str(data / "serve")
    assert tools["serve"]["dispatcher"] == str(home / "scripts/start-model.sh")
    assert tools["serve"]["default_cwd"] == str(data / "work")
    assert tools["rag"]["db_path"] == str(data / "rag.db")
    assert tools["research"]["db_path"] == str(data / "research.db")
    assert tools["test"]["python"] == str(home / ".venv/bin/python")
    assert tools["test"]["project_root"] == str(home)
    assert tools["test"]["workdir_root"] == str(data / "test-runs")
    for entry in cfg["processes"].values():
        cmd = entry["command"]
        assert cmd.startswith(str(home / "scripts") + "/"), cmd
    # Host-specific values ship neutral: env_setup keeps its $ORCH_LLAMA
    # placeholder (expanded at launch time), no llama-server binaries seeded.
    assert cfg["tools"]["serve"]["env_setup"] == "$ORCH_LLAMA/rdna4-env.sh"
    assert cfg["models"]["binaries"] == {}


# ---- B8: keyless LiteLLM mode ---------------------------------------------------

def _client() -> ModelClientMixin:
    # _auth_headers only reads the environment; no __init__ state needed.
    return ModelClientMixin.__new__(ModelClientMixin)


def test_keyless_proxy_sends_no_authorization_header(monkeypatch):
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    headers = _client()._auth_headers()
    assert "Authorization" not in headers


def test_empty_key_counts_as_unset(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "")
    assert "Authorization" not in _client()._auth_headers()


def test_authorization_header_present_when_key_set(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test")
    assert (_client()._auth_headers()
            == {"Authorization": "Bearer sk-test"})
