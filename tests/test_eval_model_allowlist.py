"""Run-local eval model allowlist: aliases, strength routes and direct calls."""
import asyncio
import copy

from runtime.preset_store import alias_allowed, preset_allowed
from runtime.tool_base import ToolContext
from tools.llm.cloud_models import _call_via_litellm
from tools.model import catalog
from tools.model.catalog import ModelUse

CFG = {
    "models": {
        "slots": {"brain": "brain", "specialist": "coder"},
        "presets": {
            "brain": {"alias": "local-orchestrator", "port": 8090, "gpu": "0,1",
                      "strengths": ["reasoning"]},
            "coder": {"alias": "local-specialist", "port": 8080, "gpu": "0,1",
                      "strengths": ["coding"]},
            "other": {"alias": "local-other", "port": 8081, "gpu": "1",
                      "strengths": ["security"]},
        },
    }
}


def test_alias_allowlist_defaults_open_and_brain_only_denies_side_channels():
    assert preset_allowed(CFG, "anything") is True
    assert alias_allowed(CFG, "local-specialist") is True
    assert alias_allowed(CFG, "glm-5.2") is True

    cfg = copy.deepcopy(CFG)
    cfg["models"]["allowed_presets"] = ["brain"]
    assert preset_allowed(cfg, "brain") is True
    assert preset_allowed(cfg, "coder") is False
    assert alias_allowed(cfg, "local-orchestrator") is True
    assert alias_allowed(cfg, None) is True
    assert alias_allowed(cfg, "local-specialist") is False
    assert alias_allowed(cfg, "glm-5.2") is False


def test_strength_route_skips_live_and_stopped_disallowed_presets(monkeypatch):
    async def fake_live_slot(config, gpu=None, slot="specialist"):
        return {"preset": "coder", "alias": "local-specialist",
                "serving": "coder", "strengths": ["coding"]}

    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)
    assert asyncio.run(catalog.strength_route(CFG, "coding")) == {
        "mode": "live", "alias": "local-specialist"}

    cfg = copy.deepcopy(CFG)
    cfg["models"]["allowed_presets"] = ["brain"]
    assert asyncio.run(catalog.strength_route(cfg, "coding")) == {}
    assert catalog.tagged_presets(cfg, "security") == []


def test_model_use_and_llm_call_refuse_disallowed_models_without_io():
    cfg = copy.deepcopy(CFG)
    cfg["models"]["allowed_presets"] = ["brain"]
    ctx = ToolContext(request_id="r", config=cfg, budget=object())

    result = asyncio.run(ModelUse().execute({"preset": "coder"}, ctx))
    assert result.status == "error"
    assert "allowed_presets" in result.error

    result = asyncio.run(_call_via_litellm(
        "local-specialist", "task", None, None, False, None, ctx))
    assert result.status == "error"
    assert "allowed_presets" in result.error
