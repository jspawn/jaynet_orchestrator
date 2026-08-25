"""agent.spawn model resolution: the static llm.call alias map, plus the
`litellm_alias` of any currently-running serve.start'd server (its call_hint
advertises `agent.spawn model='<alias>'`, so the name must resolve here).
Unknown names still hard-error; llm.call's own resolution is untouched."""
import asyncio
import json
from pathlib import Path

from runtime.tool_base import ToolContext
from tools.agent.spawn import AgentSpawn, _resolve_spawn_model


def _ctx(state_dir, spawned):
    async def fake_spawn(task, **kw):
        spawned.append(kw)
        return {"status": "ok", "answer": "done"}

    return ToolContext(
        request_id="t",
        config={"tools": {"serve": {"state_dir": str(state_dir)}}},
        budget=None, spawn=fake_spawn)


def _server(state_dir, name, alias, pid=4321):
    d = Path(state_dir) / name
    d.mkdir(parents=True)
    (d / "server.json").write_text(json.dumps({
        "name": name, "pid": pid, "litellm_alias": alias,
        "litellm_model_id": "mid-1", "port": 8091, "gpu": "1"}))


def test_spawn_accepts_served_alias(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.serving.pid_alive", lambda pid: True)
    _server(tmp_path, "vision", "local-vision")
    spawned = []
    r = asyncio.run(AgentSpawn().execute(
        {"task": "t", "model": "local-vision"}, _ctx(tmp_path, spawned)))
    assert r.status == "ok"
    assert spawned[0]["model"] == "local-vision"      # passed through unchanged


def test_spawn_rejects_unknown_model(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.serving.pid_alive", lambda pid: True)
    _server(tmp_path, "vision", "local-vision")
    spawned = []
    r = asyncio.run(AgentSpawn().execute(
        {"task": "t", "model": "not-a-model"}, _ctx(tmp_path, spawned)))
    assert r.status == "error" and "unknown model" in r.error
    assert not spawned                                # never reaches ctx.spawn


def test_spawn_static_aliases_still_resolve(tmp_path):
    spawned = []
    r = asyncio.run(AgentSpawn().execute(
        {"task": "t", "model": "glm"}, _ctx(tmp_path, spawned)))
    assert r.status == "ok" and spawned[0]["model"] == "glm-5.2"


def test_dead_server_alias_does_not_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.serving.pid_alive", lambda pid: False)
    _server(tmp_path, "vision", "local-vision")
    assert _resolve_spawn_model("local-vision", _ctx(tmp_path, [])) is None


def test_unregistered_server_alias_does_not_resolve(tmp_path, monkeypatch):
    """A live server that was never registered with LiteLLM (litellm_alias None)
    is not a spawn target — the name wouldn't route at the proxy."""
    monkeypatch.setattr("runtime.serving.pid_alive", lambda pid: True)
    _server(tmp_path, "raw", None)
    assert _resolve_spawn_model("raw", _ctx(tmp_path, [])) is None


# ---- strength routing: capability tag → live specialist alias -------------------

def _strength_ctx(spawned, presets, registry=None):
    async def fake_spawn(task, **kw):
        spawned.append(kw)
        return {"status": "ok", "answer": "done"}

    return ToolContext(
        request_id="t",
        config={"models": {"presets": presets,
                           "strengths": registry or {}}},
        budget=None, spawn=fake_spawn)


def _patch_live_slot(monkeypatch, by_slot):
    async def fake(config, gpu=None, slot="specialist"):
        return by_slot.get(slot)
    monkeypatch.setattr("tools.model.catalog.live_slot", fake)


def test_spawn_strength_routes_to_tagged_live_slot(monkeypatch):
    """strength='coding' asks for the capability, the harness picks the live
    model carrying the tag — the brain never names an alias."""
    _patch_live_slot(monkeypatch, {
        "specialist": {"preset": "coder", "serving": "qwen27b",
                       "strengths": ["coding"], "alias": "local-specialist"}})
    spawned = []
    r = asyncio.run(AgentSpawn().execute(
        {"task": "t", "strength": "coding"},
        _strength_ctx(spawned, {"coder": {"alias": "local-specialist",
                                          "strengths": ["coding"]}})))
    assert r.status == "ok"
    assert spawned[0]["model"] == "local-specialist"
    assert r.result["routed"] == "strength 'coding' → local-specialist"


def test_spawn_explicit_model_wins_over_strength(monkeypatch):
    _patch_live_slot(monkeypatch, {
        "specialist": {"preset": "coder", "serving": "qwen27b",
                       "strengths": ["coding"], "alias": "local-specialist"}})
    spawned = []
    r = asyncio.run(AgentSpawn().execute(
        {"task": "t", "model": "glm", "strength": "coding"},
        _strength_ctx(spawned, {"coder": {"alias": "local-specialist",
                                          "strengths": ["coding"]}})))
    assert r.status == "ok"
    assert spawned[0]["model"] == "glm-5.2"


def test_spawn_strength_tagged_but_stopped_is_actionable(monkeypatch):
    """The dolphin case: the preset carries the tag but isn't running — the
    error names it and points at model.ensure instead of failing vaguely."""
    _patch_live_slot(monkeypatch, {"specialist": None})
    spawned = []
    r = asyncio.run(AgentSpawn().execute(
        {"task": "t", "strength": "security"},
        _strength_ctx(spawned, {"dolphin": {"alias": "local-dolphin",
                                            "strengths": ["security"]}})))
    assert r.status == "error"
    assert "dolphin" in r.error and "model.ensure" in r.error
    assert not spawned


def test_spawn_strength_unknown_tag_lists_known(monkeypatch):
    _patch_live_slot(monkeypatch, {"specialist": None})
    spawned = []
    r = asyncio.run(AgentSpawn().execute(
        {"task": "t", "strength": "telepathy"},
        _strength_ctx(spawned, {}, registry={"coding": "code stuff"})))
    assert r.status == "error"
    assert "no preset tagged 'telepathy'" in r.error
    assert "coding" in r.error                       # known tags hinted
    assert not spawned
