"""Specialist capability tags: live_slot resolution, the system-prompt
injection, the code.delegate non-coding note, and the loop's overthinking
marker count. No network — the port probe and live_slot are monkeypatched;
the loop is driven with a fake model (same harness shape as
tests/test_loop_regressions.py, copied per the no-cross-test-imports rule)."""
import asyncio

import pytest
from conftest import run

from runtime.loop import AgentRuntime
from runtime.selector import ToolSelector
from runtime.tool_base import ToolContext
from tools.code.delegate import CodeDelegate
from tools.model import catalog

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 8, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "privacy": {"remote_llm_tools": []},
    "models": {"presets": {
        "specialist": {"alias": "local-specialist", "port": 8080, "gpu": "1",
                       "served_id": "qwen3.6-27b-davidau",
                       "strengths": ["coding", "allround"]},
        "agents1": {"alias": "local-specialist", "port": 8080, "gpu": "1",
                    "served_id": "agents-a1-35b",
                    "strengths": ["research", "science"]},
    }},
}


@pytest.fixture(autouse=True)
def _clear_slot_cache():
    catalog._live_slot_cache.clear()
    yield
    catalog._live_slot_cache.clear()


# ---- live_slot ----------------------------------------------------------------

def _probe(monkeypatch, mid):
    async def fake_query(base_url, api_key=None):
        return [mid] if mid else None
    monkeypatch.setattr(catalog.S, "query_model_ids", fake_query)


def test_live_slot_matches_served_preset(monkeypatch):
    _probe(monkeypatch, "agents-a1-35b")
    slot = run(catalog.live_slot(CFG))
    assert slot["preset"] == "agents1"
    assert slot["serving"] == "agents-a1-35b"
    assert slot["strengths"] == ["research", "science"]
    assert slot["alias"] == "local-specialist"


def test_live_slot_forwards_preset_api_key(monkeypatch):
    """A keyed remote slot's probe carries its key — otherwise a live keyed
    endpoint would look dead (401 → None)."""
    cfg = {"models": {"presets": {
        "remote1": {"alias": "local-specialist", "remote_host": "http://box:9000",
                    "served_id": "agents-a1-35b", "api_key_env": "TEST_SLOT_KEY",
                    "strengths": ["research"]},
    }, "slots": {"specialist": "remote1"}}}
    seen = {}

    async def fake_query(base_url, api_key=None):
        seen["key"] = api_key
        return ["agents-a1-35b"]
    monkeypatch.setattr(catalog.S, "query_model_ids", fake_query)
    monkeypatch.setenv("TEST_SLOT_KEY", "sk-test")
    slot = run(catalog.live_slot(cfg))
    assert seen["key"] == "sk-test"
    assert slot["preset"] == "remote1"


def test_live_slot_keyless_preset_does_not_shadow_keyed_one(monkeypatch):
    """Two presets on the same endpoint, key only set for the second: the
    probe must carry that key, not the first preset's None."""
    cfg = {"models": {"presets": {
        "remote-keyless": {"alias": "local-specialist",
                           "remote_host": "http://box:9000",
                           "served_id": "agents-a1-35b"},
        "remote-keyed": {"alias": "local-specialist",
                         "remote_host": "http://box:9000",
                         "served_id": "agents-a1-35b",
                         "api_key_env": "TEST_SLOT_KEY2"},
    }, "slots": {"specialist": "remote-keyless"}}}
    seen = {}

    async def fake_query(base_url, api_key=None):
        seen["key"] = api_key
        return ["agents-a1-35b"] if api_key else None
    monkeypatch.setattr(catalog.S, "query_model_ids", fake_query)
    monkeypatch.setenv("TEST_SLOT_KEY2", "sk-second")
    slot = run(catalog.live_slot(cfg))
    assert seen["key"] == "sk-second"
    assert slot is not None


def test_live_slot_port_down_returns_none(monkeypatch):
    _probe(monkeypatch, None)
    assert run(catalog.live_slot(CFG)) is None


def test_live_slot_split_preset_visible_on_both_cards(monkeypatch):
    """A preset on "0,1" (layer split) counts as live on GPU 0 AND GPU 1."""
    _probe(monkeypatch, "qwen3.6-27b-davidau")
    cfg = {"models": {"presets": {
        "big": {"alias": "local-brain", "port": 8090, "gpu": "0,1",
                "served_id": "qwen3.6-27b-davidau", "strengths": ["reasoning"]},
        "cpu-one": {"alias": "local-embed", "port": 8095, "gpu": "",
                    "served_id": "qwen3.6-27b-davidau"},
    }}}
    assert run(catalog.live_slot(cfg, "0"))["preset"] == "big"
    assert run(catalog.live_slot(cfg, "1"))["preset"] == "big"
    assert run(catalog.live_slot(cfg, "0"))["strengths"] == ["reasoning"]


def test_live_slot_default_finds_specialist_anywhere(monkeypatch):
    """Default lookup is slot/port-based: the specialist is found whether it
    sits on GPU 1, GPU 0, a split, or CPU — placement doesn't matter."""
    _probe(monkeypatch, "agents-a1-35b")
    for device in ("1", "0", "0,1", ""):
        catalog._live_slot_cache.clear()
        cfg = {"models": {
            "slots": {"specialist": "agents1"},
            "presets": {
                "agents1": {"alias": "local-specialist", "port": 8080,
                            "gpu": device, "served_id": "agents-a1-35b",
                            "strengths": ["research"]},
            }}}
        slot = run(catalog.live_slot(cfg))
        assert slot and slot["preset"] == "agents1", f"device {device!r}"


def test_live_slot_unknown_model_returns_none(monkeypatch):
    _probe(monkeypatch, "some-unlisted-model")
    assert run(catalog.live_slot(CFG)) is None


def test_live_slot_never_raises_and_caches(monkeypatch):
    calls = {"n": 0}

    async def boom(base_url, api_key=None):
        calls["n"] += 1
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(catalog.S, "query_model_ids", boom)
    assert run(catalog.live_slot(CFG)) is None
    assert run(catalog.live_slot(CFG)) is None   # TTL cache: no second probe
    assert calls["n"] == 1


# ---- loop harness (copied from test_loop_regressions.py) ----------------------

class _StubTool:
    private = False

    def __init__(self, name):
        self.name = name

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}


class _Registry:
    def __init__(self, names):
        self._tools = {n: _StubTool(n) for n in names}

    def all(self):
        return list(self._tools.values())

    def get(self, name):
        return self._tools.get(name)

    def openai_schemas(self, allowed=None):
        return [t.to_openai_schema() for n, t in self._tools.items()
                if allowed is None or n in allowed]


class _Trace:
    def start_run(self, *a, **k): pass
    def log(self, *a, **k): pass
    def finish_run(self, *a, **k): pass


def _final(text="done"):
    return {"role": "assistant", "content": text}


def _runtime(registry, script):
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.config = dict(CFG)
    rt.registry = registry
    rt.selector = ToolSelector(registry, rt.config)
    rt.trace = _Trace()
    rt.system_prompt = "test"
    rt.skill_catalog = ""
    rt.litellm_base = "http://x:4000"
    rt.model = "local-orchestrator"
    rt.cost_table = {}
    rt.brain_info = {}
    rt.vision_enabled = False
    rt._local_concurrency = {}
    rt._local_aliases = frozenset()
    rt._model_sems = {}
    rt._poll_safe = set()
    turns = list(script)
    seen = []

    async def fake_turn(messages, tools_schema, model=None, think=True, sampling=None):
        seen.append(messages)
        return {"message": turns.pop(0), "usage": {}}
    rt._model_turn = fake_turn
    return rt, seen


def _patch_slot(monkeypatch, slot):
    async def fake_live_slot(config, gpu=None, slot="specialist"):
        return slot_value
    slot_value = slot
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)


# ---- system-prompt injection ---------------------------------------------------

def test_prompt_line_present_with_strengths(monkeypatch):
    _patch_slot(monkeypatch, {"preset": "agents1", "serving": "agents-a1-35b",
                              "strengths": ["research", "science"],
                              "alias": "local-specialist"})
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    asyncio.run(rt.run("hi"))
    system = seen[0][0]["content"]
    assert "Specialist model: agents-a1-35b (strengths: research, science)" in system
    # the line is part of the cacheable system prefix — the volatile datetime
    # no longer lives there (it rides as a note before the user message)
    assert "Current date/time" not in system


def test_prompt_line_absent_when_slot_down(monkeypatch):
    _patch_slot(monkeypatch, None)
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    asyncio.run(rt.run("hi"))
    assert "Specialist model" not in seen[0][0]["content"]


# ---- code.delegate note ----------------------------------------------------------

def _delegate_ctx(spawn):
    return ToolContext(request_id="t", config=dict(CFG), budget=None, spawn=spawn)


async def _ok_spawn(task, tools=None, model=None, name=None, budget=None,
                    verify=None, todos_sync=False, work_root_path=None):
    return {"status": "ok", "answer": "done", "run_id": "s", "budget": {}}


def test_delegate_note_for_non_coding_specialist(monkeypatch):
    _patch_slot(monkeypatch, {"preset": "agents1", "serving": "agents-a1-35b",
                              "strengths": ["research"], "alias": "local-specialist"})
    r = run(CodeDelegate().execute({"task": "fix the parser"}, _delegate_ctx(_ok_spawn)))
    assert r.status == "ok"
    assert "not the coding model" in r.result["note"]
    assert "agents-a1-35b" in r.result["note"]


def test_delegate_no_note_for_coding_specialist(monkeypatch):
    _patch_slot(monkeypatch, {"preset": "specialist", "serving": "qwen3.6-27b-davidau",
                              "strengths": ["coding", "allround"],
                              "alias": "local-specialist"})
    r = run(CodeDelegate().execute({"task": "fix the parser"}, _delegate_ctx(_ok_spawn)))
    assert r.status == "ok"
    assert "not the coding model" not in (r.result.get("note") or "")


def test_delegate_no_note_on_resolution_failure(monkeypatch):
    _patch_slot(monkeypatch, None)
    r = run(CodeDelegate().execute({"task": "fix the parser"}, _delegate_ctx(_ok_spawn)))
    assert r.status == "ok"
    assert "not the coding model" not in (r.result.get("note") or "")


# ---- code.delegate strength routing --------------------------------------------------

def _patch_slots(monkeypatch, by_slot):
    """live_slot keyed by slot name (specialist / specialist2 / specialist3)."""
    async def fake_live_slot(config, gpu=None, slot="specialist"):
        return by_slot.get(slot)
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)


def _recording_spawn(seen):
    async def spawn(task, tools=None, model=None, name=None, budget=None,
                    verify=None, todos_sync=False, work_root_path=None):
        seen["model"] = model
        return {"status": "ok", "answer": "done", "run_id": "s", "budget": {}}
    return spawn


def test_delegate_routes_to_coding_strong_slot(monkeypatch):
    """No explicit model, no config alias: coding work must land on the
    coding-strong specialist, not the default brain."""
    _patch_slots(monkeypatch, {
        "specialist": {"preset": "s", "serving": "qwen3.6-27b",
                       "strengths": ["coding", "allround"],
                       "alias": "local-specialist"}})
    seen = {}
    r = run(CodeDelegate().execute({"task": "fix the parser"},
                                   _delegate_ctx(_recording_spawn(seen))))
    assert r.status == "ok"
    assert seen["model"] == "local-specialist"
    assert r.result["model"] == "local-specialist"
    assert "routed" in r.result
    assert "not the coding model" not in (r.result.get("note") or "")


def test_delegate_exact_strength_beats_allround(monkeypatch):
    """An exact 'coding' tag wins over an 'allround' catch-all, even on a
    lower-priority slot."""
    _patch_slots(monkeypatch, {
        "specialist": {"preset": "s", "serving": "allrounder",
                       "strengths": ["allround"], "alias": "slot-1"},
        "specialist2": {"preset": "s2", "serving": "coder",
                        "strengths": ["coding"], "alias": "slot-2"}})
    seen = {}
    run(CodeDelegate().execute({"task": "fix it"}, _delegate_ctx(_recording_spawn(seen))))
    assert seen["model"] == "slot-2"


def test_delegate_allround_is_coding_capable_fallback(monkeypatch):
    """The 27B tagged only 'allround' still gets the coding work — allround
    covers coding, just loses to an exact coding tag elsewhere."""
    _patch_slots(monkeypatch, {
        "specialist": {"preset": "s", "serving": "qwen3.6-27b",
                       "strengths": ["allround"], "alias": "local-specialist"}})
    seen = {}
    run(CodeDelegate().execute({"task": "fix it"}, _delegate_ctx(_recording_spawn(seen))))
    assert seen["model"] == "local-specialist"


def test_delegate_routing_miss_falls_back_to_brain_with_note(monkeypatch):
    """Nothing coding-strong live: default brain + the honest note."""
    _patch_slots(monkeypatch, {
        "specialist": {"preset": "s", "serving": "researcher",
                       "strengths": ["research"], "alias": "local-specialist"}})
    seen = {}
    r = run(CodeDelegate().execute({"task": "fix it"},
                                   _delegate_ctx(_recording_spawn(seen))))
    assert seen["model"] is None
    assert "default brain" in r.result["model"]
    assert "no coding-strong specialist live" in r.result["note"]
    assert "not the coding model" in r.result["note"]


def test_delegate_explicit_model_skips_routing(monkeypatch):
    _patch_slots(monkeypatch, {
        "specialist": {"preset": "s", "serving": "coder",
                       "strengths": ["coding"], "alias": "local-specialist"}})
    seen = {}
    run(CodeDelegate().execute({"task": "fix it", "model": "glm-5.2"},
                               _delegate_ctx(_recording_spawn(seen))))
    assert seen["model"] == "glm-5.2"


# ---- strength registry + prompt directory -----------------------------------------

def test_strength_registry_parses_and_tolerates_junk():
    reg = catalog.strength_registry(
        {"models": {"strengths": {"coding": "code stuff", "": "dropped"}}})
    assert reg == {"coding": "code stuff"}
    assert catalog.strength_registry({}) == {}


def test_tagged_presets_finds_exact_and_allround():
    cfg = {"models": {"presets": {
        "coder": {"alias": "a1", "strengths": ["coding"]},
        "dense": {"alias": "a2", "strengths": ["allround"]},
        "researcher": {"alias": "a3", "strengths": ["research"]}}}}
    got = catalog.tagged_presets(cfg, "coding")
    assert [t["preset"] for t in got] == ["coder", "dense"]


def test_prompt_strength_directory_with_live_holders(monkeypatch):
    """The directory tells the brain what each tag means AND who currently
    holds it — 'not live' for tags nobody is serving right now."""
    _patch_slots(monkeypatch, {
        "specialist": {"preset": "s", "serving": "qwen27b",
                       "strengths": ["coding", "allround"],
                       "alias": "local-specialist"}})
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    rt.config["models"] = {**CFG["models"],
                           "strengths": {"coding": "code synthesis, debugging",
                                         "security": "security research, pentesting"}}
    asyncio.run(rt.run("hi"))
    system = seen[0][0]["content"]
    assert "Strength tags" in system
    assert "coding = code synthesis, debugging (live: local-specialist)" in system
    assert "security = security research, pentesting (not live)" in system


def test_prompt_strength_directory_absent_without_registry(monkeypatch):
    """No models.strengths configured → no directory (CFG has none)."""
    _patch_slot(monkeypatch, {"preset": "s", "serving": "qwen27b",
                              "strengths": ["coding"], "alias": "local-specialist"})
    rt, seen = _runtime(_Registry([]), [_final("ok")])
    asyncio.run(rt.run("hi"))
    assert "Strength tags" not in seen[0][0]["content"]


# ---- overthinking markers ---------------------------------------------------------

def test_overthinking_markers_counted(monkeypatch):
    _patch_slot(monkeypatch, None)
    rt, _ = _runtime(_Registry([]),
                     [_final("wait... but... alternatively, the answer is 4.")])
    out = asyncio.run(rt.run("think carefully"))
    assert out["status"] == "ok"
    assert out["overthinking_markers"] >= 3
