"""SpawnService run-local config and privacy-permission narrowing."""
import asyncio
from types import SimpleNamespace

from runtime.budget import Budget
from runtime.spawn_service import SpawnService


def test_spawn_uses_run_config_and_cannot_widen_share_private(monkeypatch):
    base = {"orchestrator": {"model": "cloud-base"}}
    patched = {"orchestrator": {"model": "cloud-patched"}, "patched": True}
    seen = {}

    async def fake_run(task, **kwargs):
        seen["child"] = kwargs
        return {"status": "ok", "answer": "done",
                "budget": {"cost_usd": 0.0, "tokens": {}}}

    runtime = SimpleNamespace(config=base, model="cloud-base", run=fake_run)
    monkeypatch.setattr("runtime.preset_store.vision_for_alias",
                        lambda config, model: seen.setdefault("vision_config", config) and False)
    monkeypatch.setattr("runtime.spawn_service.cloud_gate.spawn_gate",
                        lambda model, config, **kwargs: seen.setdefault("gate_config", config) and None)

    async def emit(*args):
        pass

    rs = SimpleNamespace(
        allowed=None, private_taint=set(),
        budget=Budget(max_iterations=4, max_wall_clock_s=100.0,
                      max_cost_usd=1.0, max_total_tokens=1000))
    svc = SpawnService(
        runtime=runtime, run_config=patched, rs=rs, emit=emit,
        depth=0, max_depth=1, brain_gate=False,
        agent_cfg={"default_sub_iterations": 2}, run_overrides={}, run_id="r",
        confirm_provider=None, ask_provider=None, share_private=False,
        auto_confirm=False, disabled_tools=set(), work_root=None,
        extra_roots=[], run_tmp=None, project_id=None, owner=None,
        is_admin=True, think=True)
    out = asyncio.run(svc.spawn("task", model="cloud-target", share_private=True))
    assert out["status"] == "ok"
    assert seen["vision_config"] is patched
    assert seen["gate_config"] is patched
    assert seen["child"]["share_private"] is False
