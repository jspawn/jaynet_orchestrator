"""Cross-config model-alias consistency.

Three places must agree or things silently break: tools/llm/cloud_models.py's
alias map (what the brain may pass), config/litellm.yaml's model_list (what the
proxy can route), and config/runtime.yaml's costs table (what the budget bills —
a missing row means silent $0). Plus: the local litellm ids must match the
served_id of the default preset on each alias, or the wrong-model-on-the-slot
check misfires.
"""
from pathlib import Path

import yaml

from tools.llm.cloud_models import _LITELLM_ALIASES, _MODEL_MAP

ROOT = Path(__file__).resolve().parent.parent


def _load(rel):
    return yaml.safe_load((ROOT / rel).read_text())


def _litellm_names():
    return {m["model_name"] for m in _load("config/litellm.yaml")["model_list"]}


def test_friendly_map_targets_exist_in_litellm():
    names = _litellm_names()
    for friendly, target in _MODEL_MAP.items():
        assert target in names, f"alias {friendly!r} -> {target!r}: no litellm entry"


def test_static_alias_set_matches_litellm():
    missing = set(_LITELLM_ALIASES) - _litellm_names()
    assert not missing, f"aliases cloud_models accepts but LiteLLM can't route: {missing}"


def test_every_litellm_model_has_a_cost_row():
    costs = set(_load("config/runtime.yaml")["costs"])
    names = _litellm_names()
    assert not (names - costs), f"no costs row (bills silent $0): {names - costs}"
    assert not (costs - names), f"orphan costs rows: {costs - names}"


def test_local_ids_match_default_preset_served_ids():
    by_alias = {m["model_name"]: m["litellm_params"]["model"]
                for m in _load("config/litellm.yaml")["model_list"]}
    presets = _load("config/runtime.yaml")["models"]["presets"]
    default_on_alias = {}
    for p in presets.values():                      # first preset on an alias wins
        if p.get("alias") and p.get("served_id"):
            default_on_alias.setdefault(p["alias"], p["served_id"])
    for alias, sid in default_on_alias.items():
        assert by_alias.get(alias) == f"openai/{sid}", (
            f"{alias}: litellm id {by_alias.get(alias)!r} != default preset served id "
            f"'openai/{sid}'")
