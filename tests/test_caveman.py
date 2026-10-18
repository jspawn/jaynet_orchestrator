"""Caveman Compression is scoped to model-internal communication only."""
from __future__ import annotations

from pathlib import Path

import yaml
from conftest import run

from runtime import caveman
from runtime.loop import AgentRuntime
from runtime.subcall import SubcallServer
from runtime.tool_base import ToolContext, ToolResult
from runtime.verify import review_delegation
from tools.council.debate import _panelist_system
from tools.llm.cloud_models import CallCloudLLM


def _config():
    return {"caveman": {"enabled": True, "reasoning": True,
                        "handoffs": True, "model_to_model": True}}


def test_scopes_and_shipped_defaults():
    assert not caveman.enabled({}, "reasoning")       # legacy config: off
    cfg = _config()
    assert all(caveman.enabled(cfg, scope) for scope in
               ("reasoning", "handoff_out", "handoff_in", "model_to_model"))
    assert not caveman.enabled({"caveman": {"enabled": False}}, "reasoning")
    assert not caveman.enabled(
        {"caveman": {"enabled": True, "handoffs": False}}, "handoff_in")
    base = caveman.append("BASE", {}, "reasoning")
    assert base == "BASE"
    assert caveman.append("BASE", cfg, "reasoning").startswith("BASE\n\n")
    shipped = yaml.safe_load(Path("config/runtime.yaml").read_text())
    assert caveman.enabled(shipped, "reasoning")
    assert caveman.enabled(shipped, "handoff_out")
    assert caveman.enabled(shipped, "model_to_model")


def _prompt(runtime, depth):
    return run(runtime._system_prompt(
        extra_system=None, work_root=None, run_tmp=Path("scratch"), depth=depth,
        eff_threshold=0, run_overrides={}, base_system="BASE"))


def test_reasoning_and_handoff_prompt_scopes():
    from types import SimpleNamespace
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.config = {**_config(), "orchestrator": {}, "agent": {}}
    rt.system_prompt, rt.skill_catalog, rt.skills = "BASE", "", {}
    parent = _prompt(rt, 0)
    child = _prompt(rt, 1)
    assert "When writing a task for another model" in parent
    assert "Your final report goes to another model" in child
    assert "Your final report goes to another model" not in parent
    assert "CHECK lines and verbatim evidence exact" in child

    thinking = SimpleNamespace(messages=[{"role": "system", "content": "BASE"}])
    assert run(rt._apply_brain_nudges(
        thinking, user_message="x", depth=1, brain_gate=False, think=True,
        emit=_emit, run_overrides={})) is True
    reasoning = thinking.messages[0]["content"]
    assert "Hidden reasoning only" in reasoning
    assert "Never use this style in user-visible commentary or final answers" in reasoning
    silent = SimpleNamespace(messages=[{"role": "system", "content": "BASE"}])
    assert run(rt._apply_brain_nudges(
        silent, user_message="x", depth=1, brain_gate=False, think=False,
        emit=_emit, run_overrides={})) is False
    assert "Hidden reasoning only" not in silent.messages[0]["content"]


class _Budget:
    def __init__(self):
        self.cost_usd = 0
        self.iterations = 0

    def check(self):
        return None

    def add_usage(self, *args, **kwargs):
        return None


def test_subcall_instruction_and_exact_output():
    seen = {}

    class Runtime:
        cost_table = {}

        async def _model_turn(self, messages, tools, **kwargs):
            seen["messages"] = messages
            seen["kwargs"] = kwargs
            return {"message": {"role": "assistant", "content": '{"x": 1}'},
                    "usage": {}}

    cfg = _config()
    cfg["orchestrator"] = {}
    server = SubcallServer(
        Runtime(), run_id="run", config=cfg, default_model="local-orchestrator",
        tainted=lambda: False, budget=_Budget(),
        emit=_emit, emit_cost=_emit_cost)
    grant = server.mint_grant()
    result = run(server._serve({"token": grant["token"], "prompt": "extract",
                                "system": "Return JSON only."}))
    assert result["text"] == '{"x": 1}'
    system = seen["messages"][0]["content"]
    assert system.startswith("Return JSON only.")
    assert "Caveman model conversation" in system
    assert "requested output format unchanged" in system
    assert seen["kwargs"]["think"] is False


async def _emit(*args, **kwargs):
    return None


async def _emit_cost(*args, **kwargs):
    return None


def test_council_panelist_scope_respects_legacy_config():
    enabled = _panelist_system("skeptic", _config())
    assert "skeptic" in enabled and "Caveman model conversation" in enabled
    assert "Caveman model conversation" not in _panelist_system("skeptic", {})


def test_delegation_review_instruction_keeps_json_contract():
    seen = {}

    async def call(config, alias, messages):
        seen["messages"] = messages
        return {"status": "ok",
                "content": '{"verdict":"pass","issues":[],"confidence":1}'}

    result = run(review_delegation(
        "do the work", "done", {}, _config(), aliases=["local-specialist"],
        call=call))
    assert result["verdict"] == "pass"
    system = seen["messages"][0]["content"]
    assert "Caveman model conversation" in system
    assert 'Answer with JSON only' in system


def test_llm_call_uses_model_conversation_scope(monkeypatch):
    seen = {}

    async def fake(alias, task, payload, system, want_json, think, ctx, images=None):
        seen["system"] = system
        return ToolResult(status="ok", result="done")

    monkeypatch.setattr("tools.llm.cloud_models._call_via_litellm", fake)
    ctx = ToolContext(request_id="r", config=_config(), budget=None,
                      spawn=object())
    result = run(CallCloudLLM().execute(
        {"model": "glm", "task": "summarize", "system": "Be exact."}, ctx))
    assert result.status == "ok"
    assert seen["system"].startswith("Be exact.")
    assert "Caveman model conversation" in seen["system"]
    ctx.spawn = None                         # direct /llm.call: user-facing
    run(CallCloudLLM().execute(
        {"model": "glm", "task": "summarize", "system": "Be exact."}, ctx))
    assert seen["system"] == "Be exact."
