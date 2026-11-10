"""Strength routing plan (tools.model.catalog.strength_route) and the
specialist.delegate behaviors built on it: auto-swap of a stopped tagged preset
instead of settling for the allround model, and the child tool-set guard
(no mutation tool -> reject instead of spawning a helpless child; live
evidence: the brain once passed tools=["lint.run"] and then wrote the code
inline itself)."""
import asyncio

import pytest

import tools.model.catalog as catalog
from runtime.tool_base import ToolResult
from tools.specialist.delegate import SpecialistDelegate


@pytest.fixture(autouse=True)
def _generation_probe_ok(monkeypatch):
    """Offline default: successful swaps pass their backend generation probe."""
    async def ok(preset, served_model, wait_s):
        return None
    monkeypatch.setattr("tools.specialist.delegate._generation_probe", ok)

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


def test_plan_swaps_allround_when_nothing_is_live(monkeypatch):
    _patch_slots(monkeypatch, {})
    plan = asyncio.run(catalog.strength_route(CFG, "research"))
    assert plan == {"mode": "swap", "alias": "local-specialist",
                    "preset": "specialist", "allround": True}


def _priority_cfg():
    return {"models": {"presets": {
        "brain": {"alias": "local-orchestrator", "port": 8090,
                  "strengths": ["reasoning"]},
        "saber": {"alias": "local-specialist", "port": 8080,
                  "strengths": ["coding"]},
        "turbo": {"alias": "local-specialist", "port": 8080,
                  "strengths": ["coding"]},
        "allround": {"alias": "local-specialist", "port": 8082,
                     "strengths": ["allround"]},
        "other-brain": {"alias": "local-orchestrator", "port": 8090,
                        "strengths": ["reasoning"]},
    }, "slots": {"brain": "brain", "specialist": "saber",
                 "specialist2": "turbo", "specialist3": "allround"}}}


def _live(preset, alias, strengths):
    return {"preset": preset, "alias": alias, "serving": preset,
            "strengths": strengths}


def test_stopped_higher_priority_exact_precedes_live_lower(monkeypatch):
    cfg = _priority_cfg()
    _patch_slots(monkeypatch, {
        "specialist": None,
        "specialist2": _live("turbo", "local-specialist2", ["coding"])})
    plan = asyncio.run(catalog.strength_route(cfg, "coding"))
    assert plan == {"mode": "swap", "alias": "local-specialist", "preset": "saber"}


def test_live_exact_routes_follow_catalog_not_slot_order(monkeypatch):
    cfg = _priority_cfg()
    _patch_slots(monkeypatch, {
        "specialist": _live("turbo", "local-specialist", ["coding"]),
        "specialist2": _live("saber", "local-specialist2", ["coding"])})
    assert asyncio.run(catalog.route_strength(cfg, "coding")) == "local-specialist2"


def test_request_fallbacks_follow_strength_then_brain_priority(monkeypatch):
    cfg = _priority_cfg()
    _patch_slots(monkeypatch, {
        "specialist": _live("saber", "local-specialist", ["coding"]),
        "specialist2": _live("turbo", "local-specialist2", ["coding"]),
        "specialist3": _live("allround", "local-specialist3", ["allround"]),
        "brain": _live("brain", "local-orchestrator", ["reasoning"])})
    chain = asyncio.run(catalog.live_strength_fallbacks(
        cfg, "coding", "local-specialist"))
    assert chain == ["local-preset-turbo", "local-preset-allround",
                     "local-preset-brain"]


def test_request_fallbacks_never_use_a_different_shared_port_occupant(monkeypatch):
    cfg = _priority_cfg()
    _patch_slots(monkeypatch, {
        "specialist": _live("saber", "local-specialist", ["coding"]),
        "brain": _live("other-brain", "local-orchestrator", ["reasoning"])})
    # turbo is stopped; other-brain occupies the preferred brain's port. Neither
    # may be addressed under a different preset's internal fallback alias.
    assert asyncio.run(catalog.live_strength_fallbacks(
        cfg, "coding", "local-specialist")) == []


def test_request_fallbacks_distinguish_same_public_alias_presets(monkeypatch):
    cfg = _priority_cfg()
    _patch_slots(monkeypatch, {
        "specialist": _live("saber", "local-specialist", ["coding"]),
        "specialist2": _live("turbo", "local-specialist", ["coding"])})
    chain = asyncio.run(catalog.live_strength_fallbacks(
        cfg, "coding", "local-specialist", primary_preset="saber"))
    assert chain == ["local-preset-turbo"]


def test_request_fallbacks_never_target_the_primary_brain_itself(monkeypatch):
    cfg = _priority_cfg()
    _patch_slots(monkeypatch, {
        "brain": _live("brain", "local-orchestrator", ["reasoning"])})
    assert asyncio.run(catalog.live_strength_fallbacks(
        cfg, "coding", "local-orchestrator", primary_preset="brain")) == []


def test_request_fallbacks_require_payload_compatible_presets(monkeypatch):
    cfg = _priority_cfg()
    presets = cfg["models"]["presets"]
    presets["saber"]["profile"] = {"think_strip": "qwen3"}
    presets["saber"]["caps"] = {"vision": True}
    presets["turbo"]["profile"] = {"think_strip": "gemma4"}
    presets["turbo"]["caps"] = {"vision": True}
    presets["allround"]["profile"] = {"think_strip": "qwen3"}
    presets["allround"]["caps"] = {"vision": True}
    presets["allround"]["backend"] = "vllm"
    presets["brain"]["profile"] = {"think_strip": "qwen3"}
    _patch_slots(monkeypatch, {
        "specialist": _live("saber", "local-specialist", ["coding"]),
        "specialist2": _live("turbo", "local-specialist2", ["coding"]),
        "specialist3": _live("allround", "local-specialist3", ["allround"]),
        "brain": _live("brain", "local-orchestrator", ["reasoning"])})
    # Different think markup, backend and vision capability each fail closed.
    assert asyncio.run(catalog.live_strength_fallbacks(
        cfg, "coding", "local-specialist", primary_preset="saber")) == []


class _Ctx:
    def __init__(self, config):
        self.config = config
        self.work_root = None
        self.request_id = "test-run"
        self.spawn_calls = []

    async def spawn(self, task, tools=None, model=None, name=None,
                    budget=None, verify=None, work_root_path=None, sampling=None):
        self.spawn_calls.append({"tools": tools, "model": model,
                                 "sampling": sampling})
        return {"status": "ok", "answer": "done", "run_id": "child-1",
                "budget": {}}


def test_delegate_rejects_mutationless_tool_override(monkeypatch):
    _patch_slots(monkeypatch, {"specialist": SPECIALIST_LIVE})
    ctx = _Ctx(CFG)
    r = asyncio.run(SpecialistDelegate().execute(
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
                                      "status": "loaded",
                                      "evicted": [{"kind": "slot",
                                                   "slot": "specialist",
                                                   "preset": "qwen-coder",
                                                   "port": 8080}]})

    restored = []

    async def fake_restore(ctx, records):
        restored.append(records)
        return ["restored qwen-coder on slot 'specialist'"]

    monkeypatch.setattr(catalog, "ModelUse", _FakeModelUse)
    monkeypatch.setattr(catalog, "restore_evicted", fake_restore)
    ctx = _Ctx(CFG)
    r = asyncio.run(SpecialistDelegate().execute(
        {"task": "write the exploit", "strength": "security"}, ctx))
    assert r.status == "ok", r.error
    assert used == [{"preset": "dolphin", "swap": True, "include_brain": True}]
    assert ctx.spawn_calls[0]["sampling"] == {"disable_fallbacks": True}
    assert ctx.spawn_calls[0]["model"] == "local-dolphin"
    assert r.result["model"] == "local-dolphin"
    assert "security" in r.result["routed"]
    assert "swapped" in r.result["swap"]
    # swap-back: the evicted specialist preset is restored after the child
    assert restored == [[{"kind": "slot", "slot": "specialist",
                          "preset": "qwen-coder", "port": 8080}]]
    assert "restored qwen-coder" in r.result["swap_back"]
    # the internal brain-eviction permit is always re-lowered afterwards
    assert getattr(ctx, "_allow_brain_evict", False) is False


def test_delegate_swap_back_disabled_by_config(monkeypatch):
    """models.swap_back: false → evicted models stay stopped, no restore."""
    import copy as _copy
    cfg = _copy.deepcopy(CFG)
    cfg.setdefault("models", {})["swap_back"] = False
    state = {"slot": SPECIALIST_LIVE}

    async def fake_live_slot(config, gpu=None, slot="specialist"):
        return state["slot"] if slot == "specialist" else None
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)

    class _FakeModelUse:
        async def execute(self, args, ctx):
            state["slot"] = {"alias": "local-dolphin",
                             "strengths": ["security"], "serving": "dolphin"}
            return ToolResult(status="ok", tool_name="model.use",
                              result={"alias": "local-dolphin",
                                      "status": "loaded",
                                      "evicted": [{"kind": "slot",
                                                   "slot": "specialist",
                                                   "preset": "qwen-coder",
                                                   "port": 8080}]})

    restored = []

    async def fake_restore(ctx, records):
        restored.append(records)
        return ["restored"]

    monkeypatch.setattr(catalog, "ModelUse", _FakeModelUse)
    monkeypatch.setattr(catalog, "restore_evicted", fake_restore)
    ctx = _Ctx(cfg)
    r = asyncio.run(SpecialistDelegate().execute(
        {"task": "write the exploit", "strength": "security"}, ctx))
    assert r.status == "ok", r.error
    assert restored == [] and "swap_back" not in r.result


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
    r = asyncio.run(SpecialistDelegate().execute(
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
    r = asyncio.run(SpecialistDelegate().execute(
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
    r = asyncio.run(SpecialistDelegate().execute(
        {"task": "write the exploit", "strength": "security"}, ctx))
    assert r.status == "ok", r.error
    # The swap DID stop the specialist, so the allround route is empty too —
    # the honest last resort is the default brain, never a child spawned
    # into a still-loading server.
    assert ctx.spawn_calls[0]["model"] is None
    assert r.result["model"] == "(default brain)"
    assert "could not swap in 'dolphin'" in r.result["swap"]


def test_delegate_accepts_read_only_tool_override(monkeypatch):
    """Analysis-only delegations ("read this and report back") need no
    mutation lane: an override whose EVERY tool is read-only spawns."""
    _patch_slots(monkeypatch, {"specialist": SPECIALIST_LIVE})
    ctx = _Ctx(CFG)
    r = asyncio.run(SpecialistDelegate().execute(
        {"task": "look at x.png and describe it",
         "tools": ["fs.read", "web.fetch"]}, ctx))
    assert r.status == "ok", r.error
    assert ctx.spawn_calls[0]["tools"] == ["fs.read", "web.fetch"]


def test_delegate_rejects_read_only_plus_unknown_tool(monkeypatch):
    """A read-only core with ONE non-read-only, non-mutation tool listed is
    still refused — the escape hatch is pure read-only sets only."""
    _patch_slots(monkeypatch, {"specialist": SPECIALIST_LIVE})
    ctx = _Ctx(CFG)
    r = asyncio.run(SpecialistDelegate().execute(
        {"task": "audit it", "tools": ["fs.read", "agent.spawn"]}, ctx))
    assert r.status == "error"
    assert "fs.write" in r.error
    assert ctx.spawn_calls == []


def test_delegate_mutation_tool_still_accepted(monkeypatch):
    """The original rule is untouched: one mutation tool suffices."""
    _patch_slots(monkeypatch, {"specialist": SPECIALIST_LIVE})
    ctx = _Ctx(CFG)
    r = asyncio.run(SpecialistDelegate().execute(
        {"task": "fix it", "tools": ["fs.read", "fs.write"]}, ctx))
    assert r.status == "ok", r.error
    assert ctx.spawn_calls[0]["tools"] == ["fs.read", "fs.write"]


def test_generation_probe_failure_restores_and_tries_next_route(monkeypatch):
    """A non-generating candidate keeps its own error in the route note, gets
    restored, and cannot block the remaining priority chain/default brain."""
    state = {"slot": SPECIALIST_LIVE}

    async def fake_live_slot(config, gpu=None, slot="specialist"):
        return state["slot"] if slot == "specialist" else None
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)

    class _FakeModelUse:
        async def execute(self, args, ctx):
            state["slot"] = {"alias": "local-dolphin",
                             "strengths": ["security"], "serving": "dolphin"}
            if args["preset"] == "specialist":
                ctx.config["tools"]["code"]["delegate"]["swap_wait_s"] = 0
            return ToolResult(status="ok", tool_name="model.use",
                              result={"evicted": [{"slot": "brain",
                                                   "preset": "occamy-q8"}]})

    restored = []

    async def fake_restore(ctx, records):
        restored.append(records)
        return ["restored occamy-q8"]

    async def probe_fails(preset, served_model, wait_s):
        return "HTTP 500: original backend failure"

    monkeypatch.setattr(catalog, "ModelUse", _FakeModelUse)
    monkeypatch.setattr(catalog, "restore_evicted", fake_restore)
    monkeypatch.setattr("tools.specialist.delegate._generation_probe", probe_fails)
    cfg = {**CFG, "tools": {"code": {"delegate": {"swap_wait_s": 5}}}}
    ctx = _Ctx(cfg)
    r = asyncio.run(SpecialistDelegate().execute(
        {"task": "write the exploit", "strength": "security"}, ctx))
    assert r.status == "ok", r.error
    assert "original backend failure" in r.result["swap"]
    assert r.result["swap_back"] == "restored occamy-q8"
    assert restored == [[{"slot": "brain", "preset": "occamy-q8"}]] * 2
    assert ctx.spawn_calls[0]["model"] is None


def test_delegate_starting_specialist_is_awaited_and_brain_restored(monkeypatch):
    """A slow ServeStart 'starting' response is pending, not a failed swap."""
    state = {"slot": SPECIALIST_LIVE}

    async def fake_live_slot(config, gpu=None, slot="specialist"):
        return state["slot"] if slot == "specialist" else None
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)
    evicted = [{"kind": "slot", "slot": "brain", "preset": "occamy-q8"}]

    class _StartingModelUse:
        async def execute(self, args, ctx):
            state["slot"] = {"preset": "dolphin", "alias": "local-dolphin",
                             "strengths": ["security"], "serving": "dolphin"}
            return ToolResult(status="ok", tool_name="model.use",
                              result={"status": "starting", "hint": "still loading",
                                      "evicted": evicted})

    restored = []

    async def fake_restore(ctx, records):
        restored.append(records)
        return ["restored occamy-q8"]

    monkeypatch.setattr(catalog, "ModelUse", _StartingModelUse)
    monkeypatch.setattr(catalog, "restore_evicted", fake_restore)
    ctx = _Ctx(CFG)
    result = asyncio.run(SpecialistDelegate().execute(
        {"task": "write the exploit", "strength": "security"}, ctx))
    assert result.status == "ok", result.error
    assert ctx.spawn_calls[0]["model"] == "local-dolphin"
    assert restored == [evicted]


def test_delegate_failed_swap_restores_before_fallback(monkeypatch):
    """The old path spawned on the evicted brain while it was still loading."""
    _patch_slots(monkeypatch, {"specialist": SPECIALIST_LIVE})
    evicted = [{"kind": "slot", "slot": "brain", "preset": "occamy-q8"}]

    class _FailedModelUse:
        async def execute(self, args, ctx):
            return ToolResult(status="error", error="launch failed",
                              result={"status": "error", "evicted": evicted})

    calls = []

    async def fake_restore(ctx, records):
        calls.append("restore")
        assert records == evicted
        return ["restored occamy-q8"]

    monkeypatch.setattr(catalog, "ModelUse", _FailedModelUse)
    monkeypatch.setattr(catalog, "restore_evicted", fake_restore)
    ctx = _Ctx(CFG)

    async def checked_spawn(task, **kwargs):
        assert calls == ["restore"]
        return await _Ctx.spawn(ctx, task, **kwargs)
    ctx.spawn = checked_spawn
    result = asyncio.run(SpecialistDelegate().execute(
        {"task": "write the exploit", "strength": "security"}, ctx))
    assert result.status == "ok", result.error
    assert calls == ["restore"]
    assert ctx.spawn_calls[0]["model"] == "local-specialist"


def test_delegate_restore_failure_prevents_any_child(monkeypatch):
    _patch_slots(monkeypatch, {"specialist": SPECIALIST_LIVE})
    evicted = [{"kind": "slot", "slot": "brain", "preset": "occamy-q8"}]

    class _FailedModelUse:
        async def execute(self, args, ctx):
            return ToolResult(status="error", error="launch failed",
                              result={"status": "error", "evicted": evicted})

    async def failed_restore(ctx, records):
        return ["FAILED to restore occamy-q8"]

    monkeypatch.setattr(catalog, "ModelUse", _FailedModelUse)
    monkeypatch.setattr(catalog, "restore_evicted", failed_restore)
    ctx = _Ctx(CFG)
    result = asyncio.run(SpecialistDelegate().execute(
        {"task": "write the exploit", "strength": "security"}, ctx))
    assert result.status == "error"
    assert "restore failed" in result.error
    assert ctx.spawn_calls == []


def test_delegate_failed_exact_load_tries_next_exact_preset(monkeypatch):
    cfg = {"models": {"presets": {
        "saber": {"alias": "local-specialist", "strengths": ["coding"]},
        "turbo": {"alias": "local-specialist", "strengths": ["coding"]},
    }}}
    state = {"slot": None}
    attempts = []

    async def fake_live_slot(config, gpu=None, slot="specialist"):
        return state["slot"] if slot == "specialist" else None
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)

    class _FakeModelUse:
        async def execute(self, args, ctx):
            attempts.append(args["preset"])
            if args["preset"] == "saber":
                return ToolResult(status="error", error="saber failed",
                                  result={"status": "error", "evicted": []})
            state["slot"] = _live("turbo", "local-specialist", ["coding"])
            return ToolResult(status="ok", result={"status": "loaded", "evicted": []})

    monkeypatch.setattr(catalog, "ModelUse", _FakeModelUse)
    ctx = _Ctx(cfg)
    result = asyncio.run(SpecialistDelegate().execute({"task": "write code"}, ctx))
    assert result.status == "ok", result.error
    assert attempts == ["saber", "turbo"]
    assert ctx.spawn_calls[0]["model"] == "local-preset-turbo"
    assert "saber failed" in result.result["swap"]
    assert "'turbo' was swapped" in result.result["swap"]


def test_delegate_same_alias_occupant_cannot_confirm_swap(monkeypatch):
    cfg = {"models": {"presets": {
        "saber": {"alias": "local-specialist", "strengths": ["coding"]},
        "turbo": {"alias": "local-specialist", "strengths": ["coding"]},
    }}, "tools": {"code": {"delegate": {"swap_wait_s": 0.01}}}}
    attempts = []
    probes = []

    async def fake_live_slot(config, gpu=None, slot="specialist"):
        return (_live("turbo", "local-specialist", ["coding"])
                if slot == "specialist" else None)
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)

    class _FakeModelUse:
        async def execute(self, args, ctx):
            attempts.append(args["preset"])
            # Report success, but the lower-priority occupant remains live.
            return ToolResult(status="ok", result={"status": "loaded", "evicted": []})

    async def no_sleep(seconds):
        return None

    async def probe(preset, served, wait_s):
        probes.append(preset)
        return None

    monkeypatch.setattr(catalog, "ModelUse", _FakeModelUse)
    monkeypatch.setattr("tools.specialist.delegate.asyncio.sleep", no_sleep)
    monkeypatch.setattr("tools.specialist.delegate._generation_probe", probe)
    ctx = _Ctx(cfg)
    result = asyncio.run(SpecialistDelegate().execute({"task": "write code"}, ctx))
    assert result.status == "ok", result.error
    assert attempts == ["saber"]
    assert probes == []                    # turbo never confirms saber's load
    assert ctx.spawn_calls[0]["model"] == "local-preset-turbo"


def test_delegate_first_live_shared_alias_uses_exact_target(monkeypatch):
    cfg = {"models": {"presets": {
        "saber": {"alias": "local-specialist", "strengths": ["coding"]},
        "turbo": {"alias": "local-specialist", "strengths": ["coding"]},
    }}}
    _patch_slots(monkeypatch, {
        "specialist": _live("saber", "local-specialist", ["coding"])})
    ctx = _Ctx(cfg)
    result = asyncio.run(SpecialistDelegate().execute({"task": "write code"}, ctx))
    assert result.status == "ok", result.error
    assert ctx.spawn_calls[0]["model"] == "local-preset-saber"


def test_delegate_first_swap_shared_alias_uses_exact_target(monkeypatch):
    cfg = {"models": {"presets": {
        "saber": {"alias": "local-specialist", "strengths": ["coding"]},
        "turbo": {"alias": "local-specialist", "strengths": ["coding"]},
    }}}
    state = {"slot": None}

    async def fake_live_slot(config, gpu=None, slot="specialist"):
        return state["slot"] if slot == "specialist" else None
    monkeypatch.setattr(catalog, "live_slot", fake_live_slot)

    class _FakeModelUse:
        async def execute(self, args, ctx):
            state["slot"] = _live("saber", "local-specialist", ["coding"])
            return ToolResult(status="ok", result={"status": "loaded", "evicted": []})

    monkeypatch.setattr(catalog, "ModelUse", _FakeModelUse)
    ctx = _Ctx(cfg)
    result = asyncio.run(SpecialistDelegate().execute({"task": "write code"}, ctx))
    assert result.status == "ok", result.error
    assert ctx.spawn_calls[0]["model"] == "local-preset-saber"
