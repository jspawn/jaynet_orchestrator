"""Versioned per-brain overlays: snapshots, identity, precedence and provenance."""
from __future__ import annotations

import copy
import sqlite3

import pytest

from runtime import brain_overlays, eval_runner, gate_prompt
from runtime.brain_overlays import BrainOverlayStore, StaleOverlayError
from runtime.eval_store import EvalStore
from runtime.preset_store import PresetStore, brain_overlay_for, load_into_config


def test_append_only_versions_idempotent_accept_stale_base_and_rollback(tmp_path):
    store = BrainOverlayStore(tmp_path / "presets.db")
    assert store.active("brain-a")["version"] == 0
    v1 = store.apply("brain-a", {"prompt_addition": "delegate early"},
                     base_version=0, proposal_id=11, experiment_id="hx-a")
    assert v1["version"] == 1 and v1["active"]
    assert store.active("brain-a")["overlay"]["prompt_addition"] == "delegate early"
    # Retrying the cross-DB accept does not append the lesson twice.
    retry = store.apply("brain-a", {"prompt_addition": "delegate early"},
                        base_version=0, proposal_id=11, experiment_id="hx-a")
    assert retry["version"] == 1 and len(store.history("brain-a")) == 1
    v2 = store.apply("brain-a", {"prompt_addition": "ask when ambiguous"},
                     base_version=1, proposal_id=12)
    assert v2["version"] == 2
    assert "delegate early" in v2["overlay"]["prompt_addition"]
    assert "ask when ambiguous" in v2["overlay"]["prompt_addition"]
    with pytest.raises(StaleOverlayError, match=r"brain-a@v1"):
        store.apply("brain-a", {"prompt_addition": "stale"}, base_version=1)
    restored = store.rollback("brain-a", 1)
    assert restored["version"] == 3 and restored["source_version"] == 1
    assert restored["overlay"]["prompt_addition"] == "delegate early"
    cleared = store.rollback("brain-a", 0)
    assert cleared["version"] == 4
    assert cleared["overlay"] == brain_overlays.empty_overlay()
    assert [v["version"] for v in store.history("brain-a")] == [4, 3, 2, 1]
    with sqlite3.connect(tmp_path / "presets.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM preset_overlay_versions "
                            "WHERE preset='brain-a' AND active=1").fetchone()[0] == 1


def test_merge_replaces_exact_targets_and_run_overrides_win():
    base = brain_overlays.clean_overlay({
        "prompt_addition": "BASE",
        "skill_additions": {"coding": "OLD SKILL"},
        "tool_descriptions": {"web.search": "OLD TOOL"},
        "config": {"architect.threshold": 2},
    })
    changes = brain_overlays.clean_overlay({
        "prompt_addition": "NEW",
        "skill_additions": {"coding": "NEW SKILL"},
        "tool_descriptions": {"web.search": "NEW TOOL"},
        "config": {"architect.threshold": 3},
    })
    merged = brain_overlays.merge_overlay(base, changes)
    assert merged["prompt_addition"] == "BASE\n\nNEW"
    assert merged["skill_additions"]["coding"] == "OLD SKILL\n\nNEW SKILL"
    assert merged["tool_descriptions"]["web.search"] == "NEW TOOL"
    assert merged["config"]["architect.threshold"] == 3
    assert base["prompt_addition"] == "BASE"  # inputs are not mutated
    overrides = brain_overlays.run_overrides_with_overlay(
        {"tool_descriptions": {"web.search": "EXPLICIT"},
         "skill_additions": {"coding": "EXPLICIT SKILL"}}, base)
    assert overrides["tool_descriptions"]["web.search"] == "EXPLICIT"
    assert overrides["skill_additions"]["coding"] == "EXPLICIT SKILL"
    with pytest.raises(ValueError, match="not allowed"):
        brain_overlays.clean_overlay({"config": {"trace.db_path": 1}})
    from web.routes_brain_overlays import _lesson_count
    assert _lesson_count("- one\n- two\n\n- three") == 3


def test_preset_hot_load_and_shared_alias_uses_active_slot(tmp_path):
    db = tmp_path / "presets.db"
    presets = PresetStore(db)
    presets.upsert("old-brain", {"alias": "local-orchestrator"}, create=True)
    presets.upsert("new-brain", {"alias": "local-orchestrator"}, create=True)
    presets.set_slot("brain", "new-brain")
    overlays = BrainOverlayStore(db)
    overlays.apply("old-brain", {"prompt_addition": "WRONG BRAIN"}, base_version=0)
    overlays.apply("new-brain", {"prompt_addition": "RIGHT BRAIN"}, base_version=0)
    cfg = {"models": {"presets_db": str(db)}}
    assert load_into_config(cfg)
    meta = brain_overlay_for(cfg, "local-orchestrator")
    assert meta["preset"] == "new-brain" and meta["version"] == 1
    assert meta["overlay"]["prompt_addition"] == "RIGHT BRAIN"
    assert cfg["models"]["presets"]["old-brain"]["overlay"]["prompt_addition"] == \
        "WRONG BRAIN"
    overlays.apply("new-brain", {"prompt_addition": "V2"}, base_version=1)
    assert load_into_config(cfg)
    assert brain_overlay_for(cfg, "local-orchestrator")["version"] == 2


def test_eval_config_layers_active_brain_then_experiment_candidate():
    cfg = {
        "orchestrator": {"model": "local-orchestrator"},
        "eval": {"max_cost_usd": 1.0, "suite_max_cost_usd": 5.0},
        "models": {"slots": {"brain": "brain-a"}, "presets": {"brain-a": {
            "alias": "local-orchestrator", "overlay_version": 2,
            "overlay": {"config": {"eval.max_cost_usd": 0.8,
                                   "eval.suite_max_cost_usd": 4.0}},
        }}},
    }
    resolved = eval_runner.config(cfg)
    assert resolved["max_cost_usd"] == 0.8 and resolved["suite_max_cost_usd"] == 4.0
    candidate = {"config": {"key": "eval.max_cost_usd", "value": 0.4}}
    resolved = eval_runner.config(cfg, candidate)
    assert resolved["max_cost_usd"] == 0.4 and resolved["suite_max_cost_usd"] == 4.0


def test_eval_store_scopes_dedup_and_records_overlay_provenance(tmp_path):
    store = EvalStore(tmp_path / "eval.db")
    first = store.add_proposal(
        test_id="case", result_id=1, classification="prompt-tweak",
        what="w", cause="c", fix="same", brain_preset="brain-a",
        base_overlay_version=0)
    other = store.add_proposal(
        test_id="case", result_id=2, classification="prompt-tweak",
        what="w", cause="c", fix="same", brain_preset="brain-b",
        base_overlay_version=3)
    assert first and other and first["id"] != other["id"]
    duplicate = store.add_proposal(
        test_id="case", result_id=3, classification="prompt-tweak",
        what="w", cause="c", fix="same", brain_preset="brain-a",
        base_overlay_version=0)
    assert duplicate and duplicate["id"] == first["id"]
    row = store.record_result(
        test_id="case", passed=True, score=9, judge_notes="n", judge_model="j",
        cost_usd=0, tokens=1, elapsed_s=1, status="ok", run_ids=[], transcript=[],
        overlay_version=4, overlay_hash="abc")
    assert row["overlay_version"] == 4 and row["overlay_hash"] == "abc"
    store.close()


def test_result_provenance_hashes_effective_active_and_candidate_state(monkeypatch):
    monkeypatch.setattr(gate_prompt, "load", lambda *args: ("BASE", "test"))
    monkeypatch.setattr(eval_runner, "_git_provenance", lambda: ("abc", 0))
    cfg = {
        "orchestrator": {"model": "local-orchestrator"},
        "architect": {"threshold": 1},
        "models": {"slots": {"brain": "brain-a"}, "presets": {"brain-a": {
            "alias": "local-orchestrator", "overlay_version": 2,
            "overlay_hash": "overlay-sha",
            "overlay": {"prompt_addition": "ACTIVE",
                        "config": {"architect.threshold": 2}},
        }}},
    }
    active = eval_runner._provenance(cfg)
    assert active["overlay_version"] == 2 and active["overlay_hash"] == "overlay-sha"
    candidate = eval_runner._provenance(cfg, {
        "prompt_addition": "CANDIDATE",
        "config": {"key": "architect.threshold", "value": 3},
    })
    assert candidate["prompt_hash"] != active["prompt_hash"]
    assert candidate["config_hash"] != active["config_hash"]
    plain = copy.deepcopy(cfg)
    plain["models"]["presets"]["brain-a"].pop("overlay")
    assert eval_runner._provenance(plain)["prompt_hash"] != active["prompt_hash"]
