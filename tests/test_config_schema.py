"""Typed config schema (code audit 2026-09-23, item 8): the agent/budgets/
tool_selection/eval sections of runtime.yaml as pydantic models, wired into
the load path as coerce-in-place + soft warnings (never a hard failure).

Coverage: every shipped runtime.yaml key validates against the models (and
the model defaults match the shipped values — drift guard mirroring
test_config_help's config-help.yaml guard), string-ints coerce in place,
unknown nested keys warn with a did-you-mean hint and pass through,
uncoercible values warn and keep the raw value, and the admin config save
carries the warnings in its response payload.
"""
from pathlib import Path

import pytest
import yaml

from runtime.config_loader import load_config
from runtime.config_schema import SECTION_MODELS, validate_typed_sections

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = yaml.safe_load((ROOT / "config" / "runtime.yaml")
                         .read_text(encoding="utf-8"))

# Keys the runtime injects into a section at boot (never present in yaml) —
# excluded from the defaults-vs-shipped comparison.
RUNTIME_INJECTED = {"models": {"slots", "gpu_info", "cloud"}}

# Keys the loader itself rewrites (relative path → absolute under the data
# dir) — excluded from the load-is-unchanged comparison. processes gets
# per-slot treatment: each slot's `command` is absolutized.
LOADER_REWRITTEN = {"models": {"presets_db"},
                    "processes": {"command"}}


def test_shipped_runtime_yaml_validates_clean():
    """Drift guard: every key of the shipped runtime.yaml's four typed
    sections must exist in the models (a new yaml key without a model field
    warns here), and no shipped value needs a warning."""
    cfg = {s: yaml.safe_load(yaml.safe_dump(SHIPPED[s])) for s in SECTION_MODELS}
    assert validate_typed_sections(cfg) == []


def test_model_defaults_match_shipped_values():
    """Field names/types/defaults mirror the shipped runtime.yaml exactly —
    model_dump() of a bare instance equals the shipped section. Runtime-
    injected keys (models.slots/gpu_info come from the presets DB at boot,
    never from yaml) are excluded from the dump comparison."""
    for section, model in SECTION_MODELS.items():
        exclude = RUNTIME_INJECTED.get(section, set())
        assert model().model_dump(exclude=exclude) == SHIPPED[section], \
            section


def test_shipped_yaml_load_is_unchanged():
    """Coercion of already-well-typed shipped values is a no-op. The loader
    itself rewrites a few values independent of the typed coercion — relative
    paths to absolute under the data dir (all of `web`, models.presets_db) —
    those are excluded; everything else must compare equal."""
    cfg = load_config(ROOT / "config" / "runtime.yaml")
    for section in SECTION_MODELS:
        if section == "web":
            continue
        left = dict(cfg[section])
        right = dict(SHIPPED[section])
        for k in LOADER_REWRITTEN.get(section, set()):
            if section == "processes":
                for slot in left:
                    left[slot] = {kk: vv for kk, vv in left[slot].items()
                                  if kk != k}
                for slot in right:
                    right[slot] = {kk: vv for kk, vv in right[slot].items()
                                   if kk != k}
            else:
                left.pop(k, None)
                right.pop(k, None)
        assert left == right, section


def test_string_int_coerces_in_place():
    """The classic hand-coercion case: max_iterations: "40" becomes the int
    40 inside the dict — consumers stay on plain dicts, no code change."""
    cfg = {"budgets": {"max_iterations": "40"}}
    assert validate_typed_sections(cfg) == []
    assert cfg["budgets"]["max_iterations"] == 40


def test_unknown_key_warns_with_suggestion_and_passes_through():
    """agent.verify_delegate_chek (the audit's example): a warning naming
    the right key, the typo'd key kept untouched (backcompat)."""
    cfg = {"agent": {"verify_delegate_chek": False}}
    warnings = validate_typed_sections(cfg)
    assert len(warnings) == 1
    assert "agent.verify_delegate_chek" in warnings[0]
    assert "did you mean 'verify_delegate_check'" in warnings[0]
    assert cfg["agent"]["verify_delegate_chek"] is False


def test_unknown_nested_key_warns_with_suggestion():
    """Typos one level down (agent.stall_check.aftr) are caught too."""
    cfg = {"agent": {"stall_check": {"aftr": 5}}}
    warnings = validate_typed_sections(cfg)
    assert len(warnings) == 1
    assert "agent.stall_check.aftr" in warnings[0]
    assert "did you mean 'after'" in warnings[0]
    assert cfg["agent"]["stall_check"]["aftr"] == 5


def test_extra_keys_survive_untouched():
    """Forward-compatible keys from a newer version pass through: not
    coerced, not dropped, only warned about."""
    cfg = {"eval": {"future_knob": {"nested": [1, 2]}, "enabled": True}}
    warnings = validate_typed_sections(cfg)
    assert len(warnings) == 1 and "eval.future_knob" in warnings[0]
    assert cfg["eval"]["future_knob"] == {"nested": [1, 2]}
    assert cfg["eval"]["enabled"] is True


def test_uncoercible_value_warns_and_keeps_raw():
    """Current behavior preserved: a garbage value warns and stays raw —
    the downstream hand coercion handles it as before, no crash."""
    cfg = {"budgets": {"max_iterations": "lots"}}
    warnings = validate_typed_sections(cfg)
    assert len(warnings) == 1
    assert "budgets.max_iterations" in warnings[0]
    assert cfg["budgets"]["max_iterations"] == "lots"


def test_coercion_reaches_nested_sections():
    cfg = {"agent": {"stall_check": {"after": "3"}}}
    assert validate_typed_sections(cfg) == []
    assert cfg["agent"]["stall_check"]["after"] == 3


def test_load_config_validates(tmp_path):
    """The load path wires validation in: a string-int in the YAML is
    coerced by load_config, a typo'd key warns (caplog) but loads."""
    p = tmp_path / "runtime.yaml"
    p.write_text("budgets:\n  max_iterations: '40'\n"
                 "agent:\n  verify_delegate_chek: true\n")
    cfg = load_config(p)
    assert cfg["budgets"]["max_iterations"] == 40
    assert cfg["agent"]["verify_delegate_chek"] is True


def test_validate_never_raises_on_junk():
    """Backcompat floor: non-dict sections, None values, whole missing
    sections — all tolerated silently."""
    assert validate_typed_sections({"agent": None, "budgets": "x"}) == []
    assert validate_typed_sections({}) == []
    assert validate_typed_sections(None) == []


@pytest.mark.asyncio
async def test_admin_config_save_carries_warnings(web_app, web_client):
    """PUT /api/admin/config reports typed-section warnings in the payload
    (additive "warnings" key) — the save itself is never rejected."""
    app = web_app()
    async with web_client(app) as c:
        # A clean update reports no warnings.
        r = await c.put("/api/admin/config",
                        json={"updates": {"budgets.max_iterations": 45}})
        assert r.status_code == 200
        assert r.json()["warnings"] == []
        r = await c.put("/api/admin/config",
                        json={"updates": {"agent.verify_delegate_chek": True}})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert any("agent.verify_delegate_chek" in w
                   and "verify_delegate_check" in w
                   for w in body["warnings"]), body["warnings"]


@pytest.mark.asyncio
async def test_admin_config_strengths_roundtrip(web_app, web_client):
    """The Admin → Models → Strengths card rides the generic config-override
    endpoint: a new tag (models.strengths.<tag> + agent.role_temperature.<tag>)
    applies live, persists for the next runtime, and null reverts to the
    shipped YAML (a custom tag disappears, shipped tags stay)."""
    updates = {"models.strengths.zz-test": "test-only tag",
               "agent.role_temperature.zz-test": 0.15}
    app = web_app()
    async with web_client(app) as c:
        r = await c.put("/api/admin/config", json={"updates": updates})
        assert r.status_code == 200
        flat = (await c.get("/api/admin/config")).json()["config"]
        assert flat["models.strengths.zz-test"] == "test-only tag"
        assert flat["agent.role_temperature.zz-test"] == 0.15
        # Persisted as admin config overrides (users.db) — the next runtime
        # merges them at boot (web/server.py _early_users_store).
        from web.auth import UserStore
        ov = UserStore(app.state.runtime.config["web"]["users_db"]
                       ).get_config_overrides()
        assert ov == updates
        # null reverts to the shipped YAML — a custom tag disappears.
        r = await c.put("/api/admin/config",
                        json={"updates": {k: None for k in updates}})
        assert r.status_code == 200
        flat3 = (await c.get("/api/admin/config")).json()["config"]
        # The reset writes the dotpath back as None (key stays, value cleared);
        # the consumers filter the tombstone — the brain's strength directory
        # must not list it.
        assert flat3["models.strengths.zz-test"] is None
        from tools.model.catalog import strength_registry
        reg = strength_registry(app.state.runtime.config)
        assert "zz-test" not in reg and "coding" in reg
        assert UserStore(app.state.runtime.config["web"]["users_db"]
                         ).get_config_overrides() == {}
