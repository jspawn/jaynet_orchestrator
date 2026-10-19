"""Per-model harness profiles (harness lessons §4b): the preset `profile`
object — store column + validation + alias resolution — and every consumer:
think-strip family, reasoning budget override, the compaction/anchor/
sampling merge in parse_run_settings, and model-aware role_sampling.
Fallback is the contract: an unconfigured preset behaves exactly as before
profiles existed."""
import copy
import sqlite3

import pytest

from runtime.model_client import ModelClientMixin, _strip_think, _think_tag_pairs, think_family_for
from runtime.preset_store import PresetStore, _clean_profile, profile_for
from runtime.run_setup import parse_run_settings
from runtime.tool_base import role_sampling

CFG = {
    "budgets": {"max_iterations": 8, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "orchestrator": {"model": "local-orchestrator",
                     "reasoning_budget_tokens": 4096,
                     "sampling": {"temperature": 0.7, "top_p": 0.9}},
    "compaction": {"enabled": True, "max_result_chars": 2000, "keep_last": 3,
                   "every": 3},
    "agent": {"anchor": {"mode": "off", "todos_reinject": "trailing",
                         "budget": True},
              "role_temperature": {"coding": 0.2, "creative": 0.8}},
    "models": {"presets": {
        "brain-x": {"alias": "local-orchestrator",
                    "profile": {"think_strip": "gemma4",
                                "reasoning_budget_tokens": 8192,
                                "compaction": {"every": 5},
                                "anchor": {"mode": "system"},
                                "sampling": {"temperature": 0.4},
                                "role_temperature": {"coding": 0.1}}},
        "spec-y": {"alias": "local-specialist",
                   "profile": {"sampling": {"temperature": 0.3},
                               "role_temperature": {"bugtrace": 0.15}}},
        "plain": {"alias": "local-plain"},
    }},
}


# ---- profile validation + store ----

def test_clean_profile_roundtrip_and_coercions():
    p = _clean_profile({"think_strip": "QWEN3",
                        "reasoning_budget_tokens": "4096",
                        "sampling": {"temperature": 0.4},
                        "role_temperature": {"coding": "0.2"}})
    assert p["think_strip"] == "qwen3"
    assert p["reasoning_budget_tokens"] == 4096
    assert p["role_temperature"] == {"coding": 0.2}
    assert _clean_profile(None) == {} and _clean_profile({}) == {}


def test_clean_profile_rejects_bad_input():
    with pytest.raises(ValueError, match="unknown profile key"):
        _clean_profile({"thnik_strip": "qwen3"})          # typo guard
    with pytest.raises(ValueError, match="think_strip"):
        _clean_profile({"think_strip": "gpt5"})
    with pytest.raises(ValueError, match="integer"):
        _clean_profile({"reasoning_budget_tokens": "lots"})
    with pytest.raises(ValueError, match="object"):
        _clean_profile({"compaction": "often"})
    with pytest.raises(ValueError, match="object"):
        _clean_profile(["qwen3"])


def test_profile_store_roundtrip_and_column_migration(tmp_path):
    db = str(tmp_path / "presets.db")
    # A pre-profiles DB (the full original schema, no profile column) gains
    # it via ensure()'s ALTER.
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE presets("
                  "name TEXT PRIMARY KEY, role TEXT, alias TEXT, "
                  "port INTEGER, gpu TEXT, served_id TEXT, vram_gib REAL, "
                  "strengths TEXT, binary TEXT, remote_host TEXT, "
                  "backend TEXT, caps TEXT, api_key_env TEXT, "
                  "conf TEXT, source_path TEXT, updated_at REAL, "
                  "archived INTEGER NOT NULL DEFAULT 0, measured TEXT)")
    store = PresetStore(db)
    store.ensure()
    with sqlite3.connect(db) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(presets)")}
    assert "profile" in cols

    store.upsert("brain-x", {"alias": "local-orchestrator",
                             "profile": {"think_strip": "gemma4"}},
                 create=True)
    row = store.get("brain-x")
    assert row["profile"] == {"think_strip": "gemma4"}
    # a partial update leaves the stored profile alone; an explicit one
    # replaces it
    store.upsert("brain-x", {"vram_gib": 12.0})
    assert store.get("brain-x")["profile"] == {"think_strip": "gemma4"}
    store.upsert("brain-x", {"profile": {"sampling": {"temperature": 0.3}}})
    assert store.get("brain-x")["profile"] == {"sampling": {"temperature": 0.3}}


def test_profile_for_resolves_by_alias():
    p = profile_for(CFG, "local-orchestrator")
    assert p["think_strip"] == "gemma4"
    assert profile_for(CFG, "local-plain") == {}
    assert profile_for(CFG, "no-such-alias") == {}
    assert profile_for(CFG, None) == {}
    assert profile_for({}, "local-orchestrator") == {}


# ---- think-strip family ----

def test_strip_think_families():
    qwen = "a<think>x</think>b"
    g4 = "ok<|channel>thought\n<channel|>chain"
    assert _strip_think(qwen, "both") == "ab"
    assert _strip_think(g4, "both") == "ok"
    assert _strip_think(qwen, "qwen3") == "ab"
    assert _strip_think(g4, "qwen3") == g4          # other family untouched
    assert _strip_think(g4, "gemma4") == "ok"
    assert _strip_think(qwen, "gemma4") == qwen
    assert _strip_think(qwen, "none") == qwen       # pass-through
    assert _strip_think("plain", "both") == "plain"


def test_think_tag_pairs_and_family_resolution():
    assert _think_tag_pairs("both") == [("<think>", "</think>"),
                                        ("<|channel>thought\n<channel|>",
                                         "<channel|>")]
    assert _think_tag_pairs("qwen3") == [("<think>", "</think>")]
    assert _think_tag_pairs("none") == []
    assert think_family_for(CFG, "local-orchestrator") == "gemma4"
    assert think_family_for(CFG, "local-plain") == "both"      # no profile
    bad = copy.deepcopy(CFG)
    bad["models"]["presets"]["brain-x"]["profile"]["think_strip"] = "bogus"
    assert think_family_for(bad, "local-orchestrator") == "both"


# ---- reasoning budget ----

class _MC(ModelClientMixin):
    def __init__(self, config):
        self.config = config


def test_reasoning_budget_profile_overrides_global():
    mc = _MC(CFG)
    assert mc._reasoning_budget_for("local-orchestrator") == 8192
    assert mc._reasoning_budget_for("local-plain") == 4096    # global fallback
    zero = copy.deepcopy(CFG)
    zero["models"]["presets"]["brain-x"]["profile"][
        "reasoning_budget_tokens"] = 0
    # an explicit 0 in the profile disables the cap even when globally set
    assert _MC(zero)._reasoning_budget_for("local-orchestrator") is None


# ---- parse_run_settings merge ----

def _parse(config, **kw):
    kw.setdefault("run_overrides", None)
    kw.setdefault("budget_overrides", None)
    kw.setdefault("model", None)
    kw.setdefault("default_model", "local-orchestrator")
    kw.setdefault("depth", 0)
    return parse_run_settings(config, **kw)


def test_parse_settings_profile_layers_between_global_and_run_override():
    st = _parse(CFG)
    assert st.compaction["every"] == 5                 # profile over global
    assert st.compaction_every == 5
    assert st.compaction["max_result_chars"] == 2000   # global preserved
    assert st.anchor_mode == "system"                  # profile anchor
    assert st.todos_reinject == "trailing"             # global preserved
    assert st.sampling["temperature"] == 0.4           # profile over orch
    assert st.sampling["top_p"] == 0.9                 # orch preserved
    # per-run overrides still win over the profile
    st = _parse(CFG, run_overrides={"compaction": {"every": 2},
                                    "sampling": {"temperature": 0.9}})
    assert st.compaction_every == 2
    assert st.sampling["temperature"] == 0.9


def test_parse_settings_nondefault_model_gets_only_its_profile_sampling():
    # A non-default model keeps its OWN preset's pinned sampling (the
    # brain's orchestrator.sampling never leaks) — unchanged legacy behavior
    # for presets without a profile.
    st = _parse(CFG, model="local-specialist")
    assert st.sampling == {"temperature": 0.3}
    st = _parse(CFG, model="local-plain")
    assert st.sampling is None
    # sampling_force (eval variants) pins the override over the profile
    st = _parse(CFG, model="local-specialist",
                run_overrides={"sampling": {"temperature": 0.9},
                               "sampling_force": True})
    assert st.sampling["temperature"] == 0.9


# ---- model-aware role sampling ----

def test_role_sampling_profile_wins_per_tag_global_fills_the_rest():
    assert role_sampling(CFG, "coding", model="local-orchestrator") == \
        {"temperature": 0.1}                            # profile table
    assert role_sampling(CFG, "creative", model="local-orchestrator") == \
        {"temperature": 0.8}                            # global, per-tag fallback
    assert role_sampling(CFG, "bugtrace", model="local-specialist") == \
        {"temperature": 0.15}
    assert role_sampling(CFG, "bugtrace", model="local-plain") is None
    # no model passed → the legacy global-only behavior
    assert role_sampling(CFG, "coding") == {"temperature": 0.2}
