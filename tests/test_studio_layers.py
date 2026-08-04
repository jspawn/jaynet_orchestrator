"""Tests for the layered custom area (ORCH_DATA/custom).

Skills (runtime.skills) and chains (tools.chain.engine) each merge a builtin
dir with a custom dir: both appear in listings with an `origin` tag, and on a
name clash the custom artifact wins.
"""
from __future__ import annotations

import pytest
import yaml

from runtime import paths, skills
from tools.chain import engine


@pytest.fixture(autouse=True)
def _clear_skill_cache():
    skills.skills_cache_clear()
    yield
    skills.skills_cache_clear()


def _skill(root, name, description="d"):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody-{description}\n")


def _chain(root, name, description="d"):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.yaml").write_text(yaml.safe_dump(
        {"description": description,
         "steps": [{"id": "a", "prompt": "x {{input}}"}]}))


# ---- skills -----------------------------------------------------------------

def test_skills_merge_and_origin(tmp_path):
    builtin, custom = tmp_path / "builtin", tmp_path / "custom"
    _skill(builtin, "alpha")
    _skill(custom, "beta")
    merged = skills.discover_skills_layered(builtin, custom)
    assert list(merged) == ["alpha", "beta"]
    assert merged["alpha"]["origin"] == "builtin"
    assert merged["beta"]["origin"] == "custom"


def test_skills_custom_wins_on_clash(tmp_path):
    builtin, custom = tmp_path / "builtin", tmp_path / "custom"
    _skill(builtin, "alpha", "builtin-version")
    _skill(custom, "alpha", "custom-version")
    merged = skills.discover_skills_layered(builtin, custom)
    assert list(merged) == ["alpha"]
    assert merged["alpha"]["description"] == "custom-version"
    assert merged["alpha"]["origin"] == "custom"


def test_skills_missing_custom_dir_is_empty_layer(tmp_path):
    builtin = tmp_path / "builtin"
    _skill(builtin, "alpha")
    merged = skills.discover_skills_layered(builtin, tmp_path / "nope")
    assert list(merged) == ["alpha"]
    assert merged["alpha"]["origin"] == "builtin"


def test_skills_layered_cache_and_clear(tmp_path):
    builtin, custom = tmp_path / "builtin", tmp_path / "custom"
    _skill(builtin, "alpha")
    assert "beta" not in skills.discover_skills_layered_cached(builtin, custom)
    _skill(custom, "beta")
    # still cached …
    assert "beta" not in skills.discover_skills_layered_cached(builtin, custom)
    skills.skills_cache_clear()
    assert "beta" in skills.discover_skills_layered_cached(builtin, custom)


def test_load_skill_uses_custom_layer(tmp_path):
    builtin, custom = tmp_path / "builtin", tmp_path / "custom"
    _skill(builtin, "alpha", "builtin-version")
    _skill(custom, "alpha", "custom-version")
    payload = skills.load_skill(builtin, "alpha", custom_dir=custom)
    assert payload["instructions"] == "body-custom-version\n"


# ---- chains -----------------------------------------------------------------

def test_chains_list_layered(tmp_path, monkeypatch):
    builtin, custom = tmp_path / "chains", tmp_path / "custom-chains"
    monkeypatch.setattr(paths, "CUSTOM_CHAINS_DIR", custom)
    _chain(builtin, "demo", "builtin-one")
    _chain(custom, "extra", "custom-one")
    cfg = {"chains": {"dir": str(builtin)}}
    by_name = {c["name"]: c for c in engine.list_chains(cfg)}
    assert by_name["demo"]["origin"] == "builtin"
    assert by_name["extra"]["origin"] == "custom"
    assert by_name["extra"]["description"] == "custom-one"


def test_chains_custom_wins_on_clash(tmp_path, monkeypatch):
    builtin, custom = tmp_path / "chains", tmp_path / "custom-chains"
    monkeypatch.setattr(paths, "CUSTOM_CHAINS_DIR", custom)
    _chain(builtin, "demo", "builtin-version")
    _chain(custom, "demo", "custom-version")
    cfg = {"chains": {"dir": str(builtin)}}
    listed = engine.list_chains(cfg)
    assert [c["name"] for c in listed] == ["demo"]
    assert listed[0]["origin"] == "custom"
    assert listed[0]["description"] == "custom-version"
    chain = engine.load_chain(cfg, "demo")
    assert chain["description"] == "custom-version"


def test_load_chain_falls_back_to_builtin(tmp_path, monkeypatch):
    builtin, custom = tmp_path / "chains", tmp_path / "custom-chains"
    monkeypatch.setattr(paths, "CUSTOM_CHAINS_DIR", custom)
    _chain(builtin, "demo", "builtin-version")
    cfg = {"chains": {"dir": str(builtin)}}
    assert engine.load_chain(cfg, "demo")["description"] == "builtin-version"


def test_unknown_chain_error_mentions_both_dirs(tmp_path, monkeypatch):
    builtin, custom = tmp_path / "chains", tmp_path / "custom-chains"
    monkeypatch.setattr(paths, "CUSTOM_CHAINS_DIR", custom)
    _chain(builtin, "demo")
    cfg = {"chains": {"dir": str(builtin)}}
    with pytest.raises(engine.ChainError) as e:
        engine.load_chain(cfg, "nope")
    assert str(builtin) in str(e.value)
    assert str(custom) in str(e.value)
