"""Worker prompts (agent.worker_prompt): resolution chain, the delegate
wiring, and the _system_prompt base swap. No network — slot probes are
monkeypatched; the loop harness shape is copied from
tests/test_specialist_strengths.py per the no-cross-test-imports rule."""
import asyncio

import pytest
from conftest import run

import runtime.paths as paths
from runtime import worker_prompt
from runtime.loop import AgentRuntime
from runtime.selector import ToolSelector
from runtime.tool_base import ToolContext
from tools.model import catalog
from tools.specialist.delegate import SpecialistDelegate

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 8, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "privacy": {"remote_llm_tools": []},
    "models": {"presets": {
        "specialist": {"alias": "local-specialist", "port": 8080, "gpu": "1",
                       "served_id": "qwen3.6-27b-davidau",
                       "strengths": ["coding", "allround"]},
    }},
}


@pytest.fixture(autouse=True)
def _clear_slot_cache():
    catalog._live_slot_cache.clear()
    yield
    catalog._live_slot_cache.clear()


# ---- resolution chain ---------------------------------------------------------

def test_resolve_shipped_base_and_tag():
    out = worker_prompt.resolve("coding", {})
    assert "# JayNet specialist worker" in out
    assert "## Coding specialist" in out


def test_resolve_unknown_tag_falls_back_to_base_only():
    out = worker_prompt.resolve("allround", {})
    assert "# JayNet specialist worker" in out
    assert "Coding specialist" not in out


def test_resolve_custom_overlay_wins(monkeypatch, tmp_path):
    (tmp_path / "worker.md").write_text("CUSTOM BASE", encoding="utf-8")
    (tmp_path / "worker-coding.md").write_text("CUSTOM CODING", encoding="utf-8")
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path)
    out = worker_prompt.resolve("coding", {})
    assert out == "CUSTOM BASE\n\nCUSTOM CODING"


def test_resolve_config_pin_wins(monkeypatch, tmp_path):
    pin = tmp_path / "my-coder.md"
    pin.write_text("PINNED CODING", encoding="utf-8")
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "none")
    cfg = {"agent": {"worker_prompts": {"coding": str(pin)}}}
    out = worker_prompt.resolve("coding", cfg)
    assert "PINNED CODING" in out
    assert "# JayNet specialist worker" in out   # base still shipped


def test_resolve_missing_pin_falls_through(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path)
    cfg = {"agent": {"worker_prompts": {"coding": str(tmp_path / "gone.md")}}}
    out = worker_prompt.resolve("coding", cfg)
    assert "## Coding specialist" in out         # shipped, not the dead pin


def test_resolve_none_when_nothing_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    monkeypatch.setattr(paths, "HOME", tmp_path / "nohome")
    assert worker_prompt.resolve("coding", {}) is None


# ---- delegate wiring -----------------------------------------------------------

def _delegate_ctx(spawn, cfg):
    return ToolContext(request_id="t", config=cfg, budget=None, spawn=spawn)


def _patch_slots(monkeypatch):
    async def fake_live_slot(config, gpu=None, slot="specialist"):
        return {"preset": "specialist", "serving": "qwen3.6-27b-davidau",
                "strengths": ["coding", "allround"], "alias": "local-specialist"} \
            if slot == "specialist" else None
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)


def _recording_spawn(seen):
    async def spawn(task, **kwargs):
        seen.update(kwargs)
        return {"status": "ok", "answer": "done", "run_id": "s", "budget": {}}
    return spawn


def test_delegate_passes_base_system_when_enabled(monkeypatch):
    _patch_slots(monkeypatch)
    cfg = {**CFG, "agent": {"worker_prompt": True}}
    seen = {}
    r = run(SpecialistDelegate().execute({"task": "fix the parser"},
                                         _delegate_ctx(_recording_spawn(seen), cfg)))
    assert r.status == "ok"
    assert "# JayNet specialist worker" in seen["base_system"]
    assert "## Coding specialist" in seen["base_system"]
    assert "worker_prompt" in r.result          # observability marker


def test_delegate_no_base_system_when_disabled(monkeypatch):
    _patch_slots(monkeypatch)
    seen = {}
    r = run(SpecialistDelegate().execute({"task": "fix the parser"},
                                         _delegate_ctx(_recording_spawn(seen),
                                                       dict(CFG))))
    assert r.status == "ok"
    assert "base_system" not in seen
    assert "worker_prompt" not in r.result


def test_delegate_tag_module_follows_strength(monkeypatch):
    _patch_slots(monkeypatch)
    cfg = {**CFG, "agent": {"worker_prompt": True}}
    seen = {}
    run(SpecialistDelegate().execute(
        {"task": "audit the auth flow", "strength": "security"},
        _delegate_ctx(_recording_spawn(seen), cfg)))
    assert "## Security specialist" in seen["base_system"]
    assert "Coding specialist" not in seen["base_system"]


# ---- _system_prompt base swap ---------------------------------------------------

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


def _runtime():
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.config = dict(CFG)
    rt.registry = _Registry([])
    rt.selector = ToolSelector(rt.registry, rt.config)
    rt.trace = _Trace()
    rt.system_prompt = "GATE PROMPT"
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
    seen = []

    async def fake_turn(messages, tools_schema, model=None, think=True, sampling=None):
        seen.append(messages)
        return {"message": {"role": "assistant", "content": "done"}, "usage": {}}
    rt._model_turn = fake_turn
    return rt, seen


def test_worker_prompt_swaps_base_and_drops_routing_lines(monkeypatch):
    """Worker mode: the lean base replaces the gate prompt, and the brain-only
    routing blocks (specialist slot line, strength-tag directory) are omitted."""
    _patch_slots(monkeypatch)
    rt, seen = _runtime()
    rt.config["models"] = {**CFG["models"],
                           "strengths": {"coding": "code synthesis, debugging"}}
    asyncio.run(rt.run("hi", base_system="WORKER BASE"))
    system = seen[0][0]["content"]
    assert system.startswith("WORKER BASE")
    assert "GATE PROMPT" not in system
    assert "Specialist model" not in system
    assert "Strength tags" not in system


def test_no_base_system_keeps_gate_prompt_and_routing_lines(monkeypatch):
    _patch_slots(monkeypatch)
    rt, seen = _runtime()
    rt.config["models"] = {**CFG["models"],
                           "strengths": {"coding": "code synthesis, debugging"}}
    asyncio.run(rt.run("hi"))
    system = seen[0][0]["content"]
    assert system.startswith("GATE PROMPT")
    assert "Specialist model: qwen3.6-27b-davidau" in system
    assert "Strength tags" in system


# ---- admin helpers (parts/describe/save/revert) --------------------------------

@pytest.fixture
def wproots(tmp_path, monkeypatch):
    """tmp-bound HOME + CUSTOM_DIR with a shipped base + coding module."""
    home = tmp_path / "home"
    (home / "prompts").mkdir(parents=True)
    (home / "prompts" / "worker.md").write_text("SHIPPED BASE", encoding="utf-8")
    (home / "prompts" / "worker-coding.md").write_text("SHIPPED CODING",
                                                       encoding="utf-8")
    monkeypatch.setattr(paths, "HOME", home)
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    return home


def test_parts_union_and_layers(wproots, tmp_path):
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "worker-research.md").write_text("OV", encoding="utf-8")
    pin = tmp_path / "pin.md"
    pin.write_text("PIN", encoding="utf-8")
    cfg = {"agent": {"worker_prompts": {"vision": str(pin)}},
           "models": {"strengths": {"security": "pentest"},
                      "presets": {"s": {"strengths": ["multi-step", "allround"]}}}}
    parts = {p["name"]: p["layer"] for p in worker_prompt.parts(cfg)}
    assert parts == {"base": "shipped", "coding": "shipped",
                     "research": "custom", "vision": "pin",
                     "security": "none", "multi-step": "none"}


def test_describe_layers_and_editability(wproots, tmp_path):
    d = worker_prompt.describe("coding", {})
    assert d["layer"] == "shipped" and d["content"] == "SHIPPED CODING"
    assert d["editable"] is True
    worker_prompt.save_overlay("coding", "OVERLAY")
    d = worker_prompt.describe("coding", {})
    assert (d["layer"], d["content"]) == ("custom", "OVERLAY")
    pin = tmp_path / "pin.md"
    pin.write_text("PIN", encoding="utf-8")
    d = worker_prompt.describe("coding",
                               {"agent": {"worker_prompts": {"coding": str(pin)}}})
    assert (d["layer"], d["content"], d["editable"]) == ("pin", "PIN", False)
    d = worker_prompt.describe("no-such-tag", {})
    assert (d["layer"], d["content"]) == ("none", "")


def test_save_and_revert_overlay(wproots):
    p = worker_prompt.save_overlay("coding", "LIVE")
    assert p.read_text() == "LIVE"
    assert worker_prompt.resolve("coding", {}) == "SHIPPED BASE\n\nLIVE"
    assert worker_prompt.revert("coding") is True
    assert worker_prompt.revert("coding") is False
    assert worker_prompt.resolve("coding", {}) == "SHIPPED BASE\n\nSHIPPED CODING"


# ---- admin routes ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_prompt_routes(web_app, web_client, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/worker-prompts")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True       # shipped default: worker_prompt on
        names = {p["name"] for p in body["parts"]}
        assert {"base", "coding"} <= names

        r = await c.get("/api/admin/worker-prompts/base")
        assert r.status_code == 200 and r.json()["layer"] == "shipped"
        assert "# JayNet specialist worker" in r.json()["content"]

        r = await c.put("/api/admin/worker-prompts/coding",
                        json={"content": "LIVE CODING"})
        assert r.status_code == 200 and r.json()["layer"] == "custom"
        assert (tmp_path / "custom" / "worker-coding.md").read_text() == "LIVE CODING"
        assert (await c.get("/api/admin/worker-prompts/coding")).json()["layer"] == "custom"

        r = await c.delete("/api/admin/worker-prompts/coding")
        assert r.status_code == 200
        assert not (tmp_path / "custom" / "worker-coding.md").exists()
        assert (await c.delete("/api/admin/worker-prompts/coding")).status_code == 404

        r = await c.put("/api/admin/worker-prompts/BAD TAG!",
                        json={"content": "x"})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_worker_prompt_pinned_part_is_read_only(web_app, web_client,
                                                      tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    pin = tmp_path / "pin.md"
    pin.write_text("PINNED", encoding="utf-8")
    app = web_app()
    app.state.runtime.config["agent"] = {"worker_prompt": True,
                                         "worker_prompts": {"coding": str(pin)}}
    async with web_client(app) as c:
        r = await c.get("/api/admin/worker-prompts")
        assert r.json()["enabled"] is True
        r = await c.get("/api/admin/worker-prompts/coding")
        assert r.json()["layer"] == "pin" and r.json()["editable"] is False
        r = await c.put("/api/admin/worker-prompts/coding",
                        json={"content": "nope"})
        assert r.status_code == 409
