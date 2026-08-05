"""Audit S1: cloud is a property of the destination alias, not the tool name.

Covers runtime/cloud_gate.py (the shared helper), the council.debate /
eval.compare tool gates (needs_confirmation + privacy refusal), and the loop's
ctx.spawn gate end-to-end via a scripted AgentRuntime (model turns faked — no
LiteLLM involved).
"""
import json

import pytest
import yaml

from conftest import run

from runtime import cloud_gate
from runtime.tool_base import Tool, ToolResult
from tools.council.debate import CouncilDebate
from tools.eval.compare import EvalCompare

_LOCAL_CFG = {
    "orchestrator": {"model": "local-orchestrator",
                     "local_concurrency": {"local-orchestrator": 1,
                                           "local-specialist": 1}},
    "confirmation": {"enabled": True, "confirm_cloud_calls": True},
    "council": {},
    "costs": {},
}


# ---- cloud_gate helpers ------------------------------------------------------

def test_is_local_alias():
    cfg = _LOCAL_CFG
    assert cloud_gate.is_local_alias("local-orchestrator", cfg)
    assert cloud_gate.is_local_alias("local-specialist", cfg)   # prefix
    # A local backend registered under a custom alias (local_concurrency key):
    cfg2 = {"orchestrator": {"local_concurrency": {"mymodel": 2}}}
    assert cloud_gate.is_local_alias("mymodel", cfg2)
    assert not cloud_gate.is_local_alias("glm-5.2", cfg)        # cloud
    assert not cloud_gate.is_local_alias("kimi-k3", cfg)
    assert not cloud_gate.is_local_alias(None, cfg)             # fail closed
    assert not cloud_gate.is_local_alias("", cfg)
    assert not cloud_gate.is_local_alias("glm-5.2", {})         # no config: cloud


def test_spawn_gate_decision():
    cfg = _LOCAL_CFG
    g = cloud_gate.spawn_gate
    # Local targets never gate, even tainted.
    assert g(None, cfg, private_taint=True, share_private=False) is None
    assert g("local-specialist", cfg, private_taint=True, share_private=False) is None
    # Cloud + taint + no sharing -> privacy approval (never auto-confirmed).
    assert g("glm-5.2", cfg, private_taint=True, share_private=False) == "privacy"
    # Sharing allowed (or no taint) -> standard cloud confirmation.
    assert g("glm-5.2", cfg, private_taint=True, share_private=True) == "confirm"
    assert g("glm-5.2", cfg, private_taint=False, share_private=False) == "confirm"
    # confirm_cloud_calls off -> untainted cloud spawn is ungated.
    off = {"orchestrator": {}, "confirmation": {"confirm_cloud_calls": False}}
    assert g("glm-5.2", off, private_taint=False, share_private=False) is None
    # ...but the privacy gate does not depend on that switch.
    assert g("glm-5.2", off, private_taint=True, share_private=False) == "privacy"


def test_privacy_refusal_for_tools(ctx):
    c = ctx(config=_LOCAL_CFG, private_taint=True)
    msg = cloud_gate.privacy_refusal(c, ["glm-5.2"])
    assert msg and "blocked by privacy" in msg and "glm-5.2" in msg
    # Local targets are fine even when tainted.
    assert cloud_gate.privacy_refusal(c, ["local-specialist"]) is None
    # share_private waives it; no taint -> nothing to protect.
    assert cloud_gate.privacy_refusal(ctx(config=_LOCAL_CFG, private_taint=True,
                                          share_private=True), ["glm-5.2"]) is None
    assert cloud_gate.privacy_refusal(ctx(config=_LOCAL_CFG), ["glm-5.2"]) is None


# ---- council.debate ----------------------------------------------------------

def test_council_needs_confirmation_only_for_cloud(ctx):
    t = CouncilDebate()
    c = ctx(config=_LOCAL_CFG)
    local = {"topic": "x", "panel": ["local-orchestrator", "local-specialist"]}
    cloud = {"topic": "x", "panel": ["local-orchestrator", "glm-5.2"]}
    assert t.needs_confirmation(local, c) is False
    assert t.needs_confirmation(cloud, c) is True
    # A cloud synthesizer counts too.
    assert t.needs_confirmation({"topic": "x", "panel": ["local-orchestrator",
                                                         "local-specialist"],
                                 "synthesizer": "kimi-k3"}, c) is True
    # The confirm_cloud_calls switch governs the confirmation path.
    off = dict(_LOCAL_CFG, confirmation={"confirm_cloud_calls": False})
    assert t.needs_confirmation(cloud, ctx(config=off)) is False


def test_council_tainted_cloud_refused_before_any_call(ctx, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("LLM call must not happen")
    monkeypatch.setattr("tools.council.debate._call", _boom)
    c = ctx(config=_LOCAL_CFG, private_taint=True)
    r = run(CouncilDebate().execute(
        {"topic": "secret", "panel": ["local-orchestrator", "glm-5.2"]}, c))
    assert r.status == "error" and "blocked by privacy" in r.error


def test_council_local_panel_allowed_when_tainted(ctx, monkeypatch):
    calls = []

    async def fake_call(client, base, key, model, system, user, max_tokens):
        calls.append(model)
        return "a position", {}

    monkeypatch.setattr("tools.council.debate._call", fake_call)
    c = ctx(config=_LOCAL_CFG, private_taint=True)   # tainted, but all-local
    r = run(CouncilDebate().execute(
        {"topic": "secret", "panel": ["local-orchestrator", "local-specialist"],
         "rounds": 1}, c))
    assert r.status == "ok"
    assert set(calls) == {"local-orchestrator", "local-specialist"}


# ---- eval.compare ------------------------------------------------------------

def test_eval_needs_confirmation_only_for_cloud(ctx):
    t = EvalCompare()
    c = ctx(config=_LOCAL_CFG)
    assert t.needs_confirmation({"prompt": "p", "models": ["local"]}, c) is False
    assert t.needs_confirmation({"prompt": "p", "models": ["local", "glm"]}, c) is True
    off = dict(_LOCAL_CFG, confirmation={"confirm_cloud_calls": False})
    assert t.needs_confirmation({"prompt": "p", "models": ["glm"]},
                                ctx(config=off)) is False


def test_eval_tainted_cloud_refused_before_any_call(ctx, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("LLM call must not happen")
    monkeypatch.setattr("tools.eval.compare._one", _boom)
    c = ctx(config=_LOCAL_CFG, private_taint=True)
    r = run(EvalCompare().execute({"prompt": "secret", "models": ["glm"]}, c))
    assert r.status == "error" and "blocked by privacy" in r.error


def test_eval_local_allowed_when_tainted(ctx, monkeypatch):
    async def fake_one(client, model_in, messages, temperature, max_tokens,
                       want_json, c):
        return {"model": model_in, "model_name": "local-orchestrator",
                "status": "ok", "output": "x", "output_truncated": False,
                "output_chars": 1, "latency_ms": 1,
                "tokens": {"prompt": 1, "completion": 1, "cached": 0},
                "cost_usd": 0.0}
    monkeypatch.setattr("tools.eval.compare._one", fake_one)
    c = ctx(config=_LOCAL_CFG, private_taint=True)
    r = run(EvalCompare().execute({"prompt": "secret", "models": ["local"]}, c))
    assert r.status == "ok" and r.result["summary"]["succeeded"] == 1


# ---- the loop's ctx.spawn gate (agent.spawn + chain agent steps) --------------

class _Peek(Tool):
    """Private tool: its result taints the run."""
    name = "test.peek"
    private = True
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args, ctx):
        return ToolResult(status="ok", result="secret", tool_name=self.name)


class _Spawn(Tool):
    """Calls ctx.spawn like agent.spawn / a chain agent step would."""
    name = "test.spawn"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args, ctx):
        child = await ctx.spawn(args["task"], model=args.get("model"))
        ok = child.get("status") == "ok"
        return ToolResult(status="ok" if ok else "error", result=child,
                          tool_name=self.name,
                          error=None if ok else child.get("error"))


class _Provider:
    """confirm_provider stand-in: records (tool, reason), returns a verdict."""
    def __init__(self, approve):
        self.approve = approve
        self.calls = []

    async def confirm(self, run_id, tool_name, args, emit, reason=None):
        self.calls.append({"tool": tool_name, "reason": reason})
        return self.approve


def _tc(name, args, i):
    return {"id": f"c{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """An AgentRuntime with faked model turns and two test tools.

    Returns (rt, script, models_seen): pop scripted turns from `script`;
    `models_seen` records every alias a model turn was attempted with.
    """
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "sys.md").write_text("SYS")
    cfg = {
        "orchestrator": {"litellm_base": "http://127.0.0.1:1",
                         "model": "local-orchestrator",
                         "system_prompt": "prompts/sys.md",
                         "local_concurrency": {"local-orchestrator": 1,
                                               "local-specialist": 1}},
        "trace": {"db_path": str(tmp_path / "trace.db"), "log_content": False},
        "costs": {},
        "budgets": {"max_iterations": 10, "max_wall_clock_s": 60,
                    "max_cost_usd": 1.0, "max_total_tokens": 100000},
        "confirmation": {"enabled": True, "confirm_cloud_calls": True},
        "agent": {"max_depth": 2},
    }
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "runtime.yaml").write_text(yaml.safe_dump(cfg))

    from runtime.loop import AgentRuntime
    rt = AgentRuntime(cfg_dir / "runtime.yaml")
    rt.registry._tools["test.peek"] = _Peek()
    rt.registry._tools["test.spawn"] = _Spawn()

    script: list[dict] = []
    models_seen: list[str] = []

    async def fake_turn(messages, tools_schema, *a, model=None, **kw):
        models_seen.append(model or rt.model)
        resp = script.pop(0) if script else {"content": "done"}
        msg = {"role": "assistant", "content": resp.get("content")}
        if resp.get("tool_calls"):
            msg["tool_calls"] = resp["tool_calls"]
        return {"message": msg, "usage": {}}

    async def fake_turn_streaming(messages, tools_schema, on_token, *a,
                                  model=None, **kw):
        return await fake_turn(messages, tools_schema, model=model)

    monkeypatch.setattr(rt, "_model_turn", fake_turn)
    monkeypatch.setattr(rt, "_model_turn_streaming", fake_turn_streaming)
    return rt, script, models_seen


def test_spawn_local_brain_never_gated(runtime):
    rt, script, models_seen = runtime
    prov = _Provider(approve=False)      # must not even be asked
    script += [
        {"tool_calls": [_tc("test.spawn", {"task": "do", "model": "local-specialist"}, 1)]},
        {"content": "child done"},        # the child's own model turn
        {"content": "parent final"},
    ]
    res = run(rt.run("go", tools=["test.spawn"], confirm_provider=prov))
    assert res["status"] == "ok"
    assert prov.calls == []
    assert "local-specialist" in models_seen   # the child actually ran


def test_spawn_cloud_untainted_confirmed(runtime):
    rt, script, models_seen = runtime
    prov = _Provider(approve=True)
    script += [
        {"tool_calls": [_tc("test.spawn", {"task": "do", "model": "glm-5.2"}, 1)]},
        {"content": "child done"},
        {"content": "parent final"},
    ]
    res = run(rt.run("go", tools=["test.spawn"], confirm_provider=prov))
    assert res["status"] == "ok"
    assert len(prov.calls) == 1 and prov.calls[0]["reason"] is None
    assert "glm-5.2" in models_seen


def test_spawn_cloud_untainted_declined(runtime):
    rt, script, models_seen = runtime
    prov = _Provider(approve=False)
    script += [
        {"tool_calls": [_tc("test.spawn", {"task": "do", "model": "glm-5.2"}, 1)]},
        {"content": "parent final"},
    ]
    res = run(rt.run("go", tools=["test.spawn"], confirm_provider=prov))
    assert res["status"] == "ok"               # refusal is per-call, not fatal
    assert len(prov.calls) == 1
    assert "glm-5.2" not in models_seen        # child never reached the cloud


def test_spawn_cloud_tainted_privacy_declined(runtime):
    rt, script, models_seen = runtime
    prov = _Provider(approve=False)
    script += [
        {"tool_calls": [_tc("test.peek", {}, 1)]},
        {"tool_calls": [_tc("test.spawn", {"task": "do", "model": "glm-5.2"}, 2)]},
        {"content": "parent final"},
    ]
    res = run(rt.run("go", tools=["test.peek", "test.spawn"],
                     confirm_provider=prov))
    assert res["status"] == "ok"
    # Privacy approval (with reason), not the standard confirmation.
    assert len(prov.calls) == 1 and prov.calls[0]["reason"]
    assert "glm-5.2" not in models_seen


def test_spawn_cloud_tainted_privacy_approved(runtime):
    rt, script, models_seen = runtime
    prov = _Provider(approve=True)
    script += [
        {"tool_calls": [_tc("test.peek", {}, 1)]},
        {"tool_calls": [_tc("test.spawn", {"task": "do", "model": "glm-5.2"}, 2)]},
        {"content": "child done"},
        {"content": "parent final"},
    ]
    res = run(rt.run("go", tools=["test.peek", "test.spawn"],
                     confirm_provider=prov))
    assert res["status"] == "ok"
    assert len(prov.calls) == 1 and prov.calls[0]["reason"]
    assert "glm-5.2" in models_seen


def test_spawn_cloud_tainted_share_private_still_needs_cloud_confirm(runtime):
    rt, script, models_seen = runtime
    prov = _Provider(approve=True)
    script += [
        {"tool_calls": [_tc("test.peek", {}, 1)]},
        {"tool_calls": [_tc("test.spawn", {"task": "do", "model": "glm-5.2"}, 2)]},
        {"content": "child done"},
        {"content": "parent final"},
    ]
    res = run(rt.run("go", tools=["test.peek", "test.spawn"],
                     share_private=True, confirm_provider=prov))
    assert res["status"] == "ok"
    # share_private waives the PRIVACY gate; the standard cloud confirm remains.
    assert len(prov.calls) == 1 and prov.calls[0]["reason"] is None
    assert "glm-5.2" in models_seen
