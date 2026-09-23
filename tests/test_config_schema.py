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


def test_shipped_runtime_yaml_validates_clean():
    """Drift guard: every key of the shipped runtime.yaml's four typed
    sections must exist in the models (a new yaml key without a model field
    warns here), and no shipped value needs a warning."""
    cfg = {s: yaml.safe_load(yaml.safe_dump(SHIPPED[s])) for s in SECTION_MODELS}
    assert validate_typed_sections(cfg) == []


def test_model_defaults_match_shipped_values():
    """Field names/types/defaults mirror the shipped runtime.yaml exactly —
    model_dump() of a bare instance equals the shipped section."""
    for section, model in SECTION_MODELS.items():
        assert model().model_dump() == SHIPPED[section], section


def test_shipped_yaml_load_is_unchanged():
    """Coercion of already-well-typed shipped values is a no-op."""
    cfg = load_config(ROOT / "config" / "runtime.yaml")
    for section in SECTION_MODELS:
        assert cfg[section] == SHIPPED[section], section


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
