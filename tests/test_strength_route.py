"""Strength routing plan (tools.model.catalog.strength_route) and the
code.delegate behaviors built on it: auto-swap of a stopped tagged preset
instead of settling for the allround model, and the child tool-set guard
(no mutation tool -> reject instead of spawning a helpless child; live
evidence: the brain once passed tools=["lint.run"] and then wrote the code
inline itself)."""
import asyncio

import tools.model.catalog as catalog
from runtime.tool_base import ToolResult
from tools.code.delegate import CodeDelegate

CFG = {
    "models": {"presets": {
        "specialist": {"alias": "local-specialist", "port": 8080, "gpu": "1",
                       "strengths": ["coding", "allround"]},
        "dolphin": {"alias": "local-dolphin", "port": 8081, "gpu": "1",
                    "strengths": ["security"]},
        "remote-sec": {"alias": "remote-sec", "remote_host": "192.168.1.9",
                       "port": 9000, "strengths": ["forensics"]},
    }},
}


def _patch_slots(monkeypatch, slots):
    """slots: {slot_name: dict|None} — what live_slot reports per slot."""
    async def fake_live_slot(config, gpu=None, slot="specialist"):
        return slots.get(slot)
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)


SPECIALIST_LIVE = {"alias": "local-specialist",
                   "strengths": ["coding", "allround"],
                   "serving": "qwen27b"}


def test_plan_live_when_exact_holder_serving(monkeypatch):
    _patch_slots(monkeypatch, {"specialist": SPECIALIST_LIVE})
    plan = asyncio.run(catalog.strength_route(CFG, "coding"))
    assert plan == {"mode": "live", "alias": "local-specialist"}


def test_plan_swap_when_tagged_preset_stopped(monkeypatch):
    # dolphin (security) is not serving anywhere; it's a LOCAL preset ->
    # swap it in rather than settle for the allround specialist.
    _patch_slots(monkeypatch, {"specialist": SPECIALIST_LIVE})
    plan = asyncio.run(catalog.strength_route(CFG, "security"))
    assert plan == {"mode": "swap", "alias": "local-dolphin",
                    "preset": "dolphin"}


def test_plan_never_swaps_a_remote_preset(monkeypatch):
    _patch_slots(monkeypatch, {"specialist": SPECIALIST_LIVE})
    plan = asyncio.run(catalog.strength_route(CFG, "forensics"))
    # remote-sec carries the tag but JayNet can't launch off-box -> allround.
    assert plan == {"mode": "allround", "alias": "local-specialist"}


def test_plan_allround_when_no_tagged_preset(monkeypatch):
    _patch_slots(monkeypatch, {"specialist": SPECIALIST_LIVE})
    plan = asyncio.run(catalog.strength_route(CFG, "research"))
    assert plan == {"mode": "allround", "alias": "local-specialist"}


def test_plan_empty_when_nothing_live_and_no_preset(monkeypatch):
    _patch_slots(monkeypatch, {})
    plan = asyncio.run(catalog.strength_route(CFG, "research"))
    assert plan == {}


class _Ctx:
    def __init__(self, config):
        self.config = config
        self.work_root = None
        self.request_id = "test-run"
        self.spawn_calls = []

    async def spawn(self, task, tools=None, model=None, name=None,
                    budget=None, verify=None, work_root_path=None):
        self.spawn_calls.append({"tools": tools, "model": model})
        return {"status": "ok", "answer": "done", "run_id": "child-1",
                "budget": {}}


def test_delegate_rejects_mutationless_tool_override(monkeypatch):
    _patch_slots(monkeypatch, {"specialist": SPECIALIST_LIVE})
    ctx = _Ctx(CFG)
    r = asyncio.run(CodeDelegate().execute(
        {"task": "build it", "tools": ["lint.run"]}, ctx))
    assert r.status == "error"
    assert "fs.write" in r.error
    assert ctx.spawn_calls == []          # no helpless child was spawned


def test_delegate_auto_swaps_stopped_strength_holder(monkeypatch):
    # Before the swap the specialist slot serves the coding model; the fake
    # model.use flips it to dolphin, so the post-swap confirm probe passes.
    state = {"slot": SPECIALIST_LIVE}

    async def fake_live_slot(config, gpu=None, slot="specialist"):
        return state["slot"] if slot == "specialist" else None
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)

    used = []

    class _FakeModelUse:
        async def execute(self, args, ctx):
            used.append(args)
            state["slot"] = {"alias": "local-dolphin",
                             "strengths": ["security"], "serving": "dolphin"}
            return ToolResult(status="ok", tool_name="model.use",
                              result={"alias": "local-dolphin",
                                      "status": "loaded"})

    monkeypatch.setattr(catalog, "ModelUse", _FakeModelUse)
    ctx = _Ctx(CFG)
    r = asyncio.run(CodeDelegate().execute(
        {"task": "write the exploit", "strength": "security"}, ctx))
    assert r.status == "ok", r.error
    assert used == [{"preset": "dolphin", "swap": True}]
    assert ctx.spawn_calls[0]["model"] == "local-dolphin"
    assert r.result["model"] == "local-dolphin"
    assert "security" in r.result["routed"]
    assert "swapped" in r.result["swap"]


def test_delegate_swap_failure_falls_back_to_allround(monkeypatch):
    _patch_slots(monkeypatch, {"specialist": SPECIALIST_LIVE})

    class _FailingModelUse:
        async def execute(self, args, ctx):
            return ToolResult(status="ok", tool_name="model.use",
                              result={"alias": "local-dolphin",
                                      "status": "not enough VRAM",
                                      "hint": "free GPU 1"})

    monkeypatch.setattr(catalog, "ModelUse", _FailingModelUse)
    ctx = _Ctx(CFG)
    r = asyncio.run(CodeDelegate().execute(
        {"task": "write the exploit", "strength": "security"}, ctx))
    assert r.status == "ok", r.error
    assert ctx.spawn_calls[0]["model"] == "local-specialist"
    assert "could not swap in 'dolphin'" in r.result["swap"]


def test_delegate_waits_for_the_swapped_model_to_load(monkeypatch):
    """Live evidence (first v1.7.3 swap): ServeStart accepted the launch, but
    the confirm probe ran while the port was still empty — the delegate fell
    back to the brain with the specialist already stopped. The confirm must
    poll until the exact holder answers (model loads take tens of seconds)."""
    import asyncio as _aio
    state = {"swapped": False, "probes": 0}

    async def fake_live_slot(config, gpu=None, slot="specialist"):
        if slot != "specialist":
            return None
        if not state["swapped"]:
            return SPECIALIST_LIVE
        state["probes"] += 1
        if state["probes"] >= 3:                     # loaded on the 3rd probe
            return {"alias": "local-dolphin", "strengths": ["security"],
                    "serving": "dolphin"}
        return None                                  # port still empty
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)

    async def instant_sleep(_s):
        return None
    monkeypatch.setattr(_aio, "sleep", instant_sleep)

    class _SlowModelUse:
        async def execute(self, args, ctx):
            state["swapped"] = True
            return ToolResult(status="ok", tool_name="model.use",
                              result={"alias": "local-dolphin",
                                      "status": "loaded"})

    monkeypatch.setattr(catalog, "ModelUse", _SlowModelUse)
    ctx = _Ctx(CFG)
    r = asyncio.run(CodeDelegate().execute(
        {"task": "write the exploit", "strength": "security"}, ctx))
    assert r.status == "ok", r.error
    assert state["probes"] >= 3                      # it really polled
    assert ctx.spawn_calls[0]["model"] == "local-dolphin"


def test_delegate_swap_confirm_timeout_falls_back(monkeypatch):
    # swap_wait_s: 0 — the holder never answers in time → allround fallback
    # instead of spawning the child into a still-loading server.
    state = {"swapped": False}

    async def fake_live_slot(config, gpu=None, slot="specialist"):
        if slot == "specialist" and not state["swapped"]:
            return SPECIALIST_LIVE
        return None                                  # never comes live
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)

    class _SlowModelUse:
        async def execute(self, args, ctx):
            state["swapped"] = True
            return ToolResult(status="ok", tool_name="model.use",
                              result={"alias": "local-dolphin",
                                      "status": "loaded"})

    monkeypatch.setattr(catalog, "ModelUse", _SlowModelUse)
    cfg = {**CFG, "tools": {"code": {"delegate": {"swap_wait_s": 0}}}}
    ctx = _Ctx(cfg)
    r = asyncio.run(CodeDelegate().execute(
        {"task": "write the exploit", "strength": "security"}, ctx))
    assert r.status == "ok", r.error
    # The swap DID stop the specialist, so the allround route is empty too —
    # the honest last resort is the default brain, never a child spawned
    # into a still-loading server.
    assert ctx.spawn_calls[0]["model"] is None
    assert r.result["model"] == "(default brain)"
    assert "could not swap in 'dolphin'" in r.result["swap"]
