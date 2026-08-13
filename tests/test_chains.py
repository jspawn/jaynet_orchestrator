"""Tests for chains — named multi-step pipelines (chain.list / chain.run).

No model calls: prompt steps are monkeypatched at engine._call_via_litellm,
agent steps at ctx.spawn. Chains are YAML files in a throwaway dir.
"""
from __future__ import annotations

import pytest
import yaml
from conftest import run

from runtime.tool_base import ToolContext, ToolResult
from tools.chain import engine
from tools.chain.list import ChainList
from tools.chain.run import ChainRun


@pytest.fixture
def chain_dir(tmp_path):
    d = tmp_path / "chains"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _no_custom_layer(tmp_path, monkeypatch):
    """Keep these tests hermetic: the real ORCH_DATA custom dirs stay out."""
    monkeypatch.setattr("runtime.paths.CUSTOM_CHAINS_DIR",
                        tmp_path / "custom-chains")


def _ctx(chain_dir, spawn=None):
    cfg = {"chains": {"dir": str(chain_dir)},
           "tools": {"serve": {"state_dir": str(chain_dir / "serve")}}}
    return ToolContext(request_id="t", config=cfg, budget=None, spawn=spawn)


def _write(chain_dir, name, doc: dict):
    (chain_dir / f"{name}.yaml").write_text(yaml.safe_dump(doc))


def _fake_litellm(text="SUMMARY", tokens=None):
    async def fake(alias, task, payload, system, want_json, think, ctx):
        fake.calls.append({"alias": alias, "task": task, "think": think})
        return ToolResult(status="ok", result=text,
                          tokens_used=tokens or {"prompt": 10, "completion": 5,
                                                 "cached": 2})
    fake.calls = []
    return fake


# ---- loading / validation -------------------------------------------------

def test_list_empty(chain_dir):
    res = run(ChainList().execute({}, _ctx(chain_dir)))
    assert res.status == "ok"
    assert res.result["chains"] == []
    assert "no chains" in res.result["note"]


def test_list_shows_chains(chain_dir):
    _write(chain_dir, "demo", {"description": "d", "steps": [
        {"id": "a", "prompt": "hi {{input}}"}]})
    res = run(ChainList().execute({}, _ctx(chain_dir)))
    assert res.result["chains"] == [{"name": "demo", "description": "d",
                                     "steps": 1, "origin": "builtin"}]


def test_load_rejects_traversal(chain_dir):
    with pytest.raises(engine.ChainError, match="invalid chain name"):
        engine.load_chain(_ctx(chain_dir).config, "../etc/passwd")


def test_load_unknown_names_available(chain_dir):
    _write(chain_dir, "demo", {"steps": [{"id": "a", "prompt": "x"}]})
    with pytest.raises(engine.ChainError, match="Available: demo"):
        engine.load_chain(_ctx(chain_dir).config, "nope")


@pytest.mark.parametrize("doc,match", [
    ({"steps": []}, "non-empty 'steps'"),
    ({"steps": [{"id": "a"}]}, "exactly one of"),
    ({"steps": [{"id": "a", "prompt": "x", "agent": "y"}]}, "exactly one of"),
    ({"steps": [{"prompt": "x"}]}, "valid 'id'"),
    ({"steps": [{"id": "a", "prompt": "x"}, {"id": "a", "prompt": "y"}]},
     "duplicate step id"),
])
def test_load_validation(chain_dir, doc, match):
    _write(chain_dir, "bad", doc)
    with pytest.raises(engine.ChainError, match=match):
        engine.load_chain(_ctx(chain_dir).config, "bad")


# ---- prompt steps ----------------------------------------------------------

def test_prompt_chain_interpolates(chain_dir, monkeypatch):
    fake = _fake_litellm()
    monkeypatch.setattr(engine, "_call_via_litellm", fake)
    _write(chain_dir, "demo", {"steps": [
        {"id": "one", "prompt": "first about {{input}}"},
        {"id": "two", "prompt": "second sees {{steps.one.output}}"},
    ]})
    res = run(ChainRun().execute({"name": "demo", "input": "octopuses"},
                                 _ctx(chain_dir)))
    assert res.status == "ok"
    assert fake.calls[0]["task"] == "first about octopuses"
    assert fake.calls[1]["task"] == "second sees SUMMARY"
    assert res.result["output"] == "SUMMARY"
    assert [s["id"] for s in res.result["steps"]] == ["one", "two"]
    # local default model, tokens aggregated for the budget
    assert fake.calls[0]["alias"] == "local-orchestrator"
    assert res.tokens_used["prompt"] == 20
    assert res.tokens_used["completion"] == 10
    assert res.tokens_used["cached"] == 4


def test_prompt_step_explicit_local_model(chain_dir, monkeypatch):
    fake = _fake_litellm()
    monkeypatch.setattr(engine, "_call_via_litellm", fake)
    _write(chain_dir, "demo", {"steps": [
        {"id": "a", "prompt": "x {{input}}", "model": "local-specialist",
         "think": False}]})
    res = run(ChainRun().execute({"name": "demo", "input": "i"}, _ctx(chain_dir)))
    assert res.status == "ok"
    assert fake.calls[0]["alias"] == "local-specialist"
    assert fake.calls[0]["think"] is False


def test_prompt_step_refuses_cloud(chain_dir):
    _write(chain_dir, "demo", {"steps": [
        {"id": "a", "prompt": "x", "model": "kimi"}]})
    res = run(ChainRun().execute({"name": "demo", "input": "i"}, _ctx(chain_dir)))
    assert res.status == "error"
    assert "LOCAL-only" in res.error
    assert "llm.call" in res.error


def test_unknown_placeholder_errors(chain_dir, monkeypatch):
    monkeypatch.setattr(engine, "_call_via_litellm", _fake_litellm())
    _write(chain_dir, "demo", {"steps": [
        {"id": "a", "prompt": "see {{steps.missing.output}}"}]})
    res = run(ChainRun().execute({"name": "demo", "input": "i"}, _ctx(chain_dir)))
    assert res.status == "error"
    assert "unknown placeholder" in res.error
    assert "steps.missing.output" in res.error


# ---- agent steps -----------------------------------------------------------

def test_agent_step_uses_spawn(chain_dir, monkeypatch):
    fake = _fake_litellm()
    monkeypatch.setattr(engine, "_call_via_litellm", fake)
    spawned = {}

    async def fake_spawn(task, **kw):
        spawned.update(task=task, **kw)
        return {"status": "ok", "answer": "FACTS", "run_id": "r1"}
    _write(chain_dir, "demo", {"steps": [
        {"id": "research", "agent": "research {{input}}",
         "tools": ["web.search"], "model": "local-specialist"},
        {"id": "brief", "prompt": "brief from {{steps.research.output}}"},
    ]})
    res = run(ChainRun().execute({"name": "demo", "input": "stars"},
                                 _ctx(chain_dir, spawn=fake_spawn)))
    assert res.status == "ok"
    assert spawned["task"] == "research stars"
    assert spawned["tools"] == ["web.search"]
    assert spawned["model"] == "local-specialist"
    assert spawned["name"] == "demo/research"
    assert fake.calls[0]["task"] == "brief from FACTS"
    assert res.result["steps"][0]["sub_run_id"] == "r1"


def test_agent_step_failure_stops_chain(chain_dir):
    async def failing_spawn(task, **kw):
        return {"status": "error", "error": "budget exceeded"}
    _write(chain_dir, "demo", {"steps": [
        {"id": "a", "agent": "do {{input}}"},
        {"id": "b", "prompt": "never reached"}]})
    res = run(ChainRun().execute({"name": "demo", "input": "i"},
                                 _ctx(chain_dir, spawn=failing_spawn)))
    assert res.status == "error"
    assert "budget exceeded" in res.error
    assert "step 'a'" in res.error


def test_agent_step_without_spawn_seam(chain_dir):
    _write(chain_dir, "demo", {"steps": [{"id": "a", "agent": "x"}]})
    res = run(ChainRun().execute({"name": "demo", "input": "i"},
                                 _ctx(chain_dir, spawn=None)))
    assert res.status == "error"
    assert "not available" in res.error


# ---- the tool envelope ------------------------------------------------------

def test_run_requires_input(chain_dir):
    res = run(ChainRun().execute({"name": "x", "input": "  "}, _ctx(chain_dir)))
    assert res.status == "error"
    assert "input is required" in res.error


def test_run_private_flag():
    assert ChainRun().private is True
