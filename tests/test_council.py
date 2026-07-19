"""council.debate: multi-round flow, personas, synthesis, response parsing,
run-budget charging of the direct-to-LiteLLM calls."""
import asyncio
import tools.council.debate as M
from tools.council.debate import CouncilDebate, _normalize_panel, _call
from runtime.budget import Budget
from runtime.tool_base import ToolContext

CFG = {"orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
       "council": {"panel": ["local-orchestrator", "local-coder"], "rounds": 2, "max_tokens": 500}}
def _ctx(): return ToolContext(request_id="t", config=CFG, budget=None)
def _run(t, args): return asyncio.run(t.execute(args, _ctx()))


def test_normalize_panel():
    p = _normalize_panel(["a", {"model": "b", "persona": "skeptic"}, {"persona": "nomodel"}])
    assert p == [{"model": "a", "persona": None}, {"model": "b", "persona": "skeptic"}]

def test_debate_flow(monkeypatch):
    calls = []
    async def fake(client, base, key, model, system, user, max_tokens):
        calls.append({"model": model, "user": user, "system": system})
        return ("SYNTH" if "Synthesize this" in user else f"POS[{model}]"), {}
    monkeypatch.setattr(M, "_call", fake)
    r = _run(CouncilDebate(), {"topic": "Migrate ERP to cloud?", "rounds": 2})
    assert r.status == "ok"
    assert [p["model"] for p in r.result["panel"]] == ["local-orchestrator", "local-coder"]  # default panel
    assert len(calls) == 2 * 2 + 1        # 2 rounds x 2 panelists + 1 synthesis
    panel_calls = [c for c in calls if "Synthesize this" not in c["user"]]
    assert all("OPENING position" in c["user"] for c in panel_calls[:2])       # round 1 opens
    assert all("other panelists said" in c["user"] for c in panel_calls[2:4])  # round 2 rebuts
    synth = [c for c in calls if "Synthesize this" in c["user"]][0]
    assert synth["model"] == "local-orchestrator"      # brain synthesizes
    assert r.result["synthesis"] == "SYNTH" and len(r.result["transcript"]) == 2
    assert len(r.result["final_positions"]) == 2

def test_rebuttal_sees_others(monkeypatch):
    seen = []
    async def fake(client, base, key, model, system, user, max_tokens):
        seen.append((model, user))
        return ("SYNTH" if "Synthesize this" in user else f"stance-of-{model}"), {}
    monkeypatch.setattr(M, "_call", fake)
    _run(CouncilDebate(), {"topic": "X", "rounds": 2, "panel": ["m1", "m2"]})
    # in round 2, m1's prompt must contain m2's round-1 stance
    r2_m1 = [u for m, u in seen if m == "m1" and "other panelists said" in u][0]
    assert "stance-of-m2" in r2_m1

def test_personas_in_system(monkeypatch):
    seen = []
    async def fake(client, base, key, model, system, user, max_tokens):
        seen.append((model, system)); return ("S" if "Synthesize this" in user else "x"), {}
    monkeypatch.setattr(M, "_call", fake)
    _run(CouncilDebate(), {"topic": "X", "rounds": 1,
                           "panel": [{"model": "m1", "persona": "cautious risk officer"},
                                     {"model": "m2", "persona": "pragmatic engineer"}]})
    sysmap = {m: s for m, s in seen if m in ("m1", "m2")}
    assert "cautious risk officer" in sysmap["m1"] and "pragmatic engineer" in sysmap["m2"]

def test_call_parsing():
    class _R:
        def __init__(s, m): s._m = m
        def raise_for_status(s): pass
        def json(s): return {"choices": [{"message": s._m}], "usage": {"prompt_tokens": 3}}
    class _C:
        def __init__(s, m): s._m = m
        async def post(s, *a, **k): return _R(s._m)
    assert asyncio.run(_call(_C({"content": "hi", "role": "assistant"}), "b", "k", "m", "s", "u", 10)) == ("hi", {"prompt_tokens": 3})
    # only reasoning_content -> fallback
    assert asyncio.run(_call(_C({"content": "", "reasoning_content": "thinking"}), "b", "k", "m", "s", "u", 10)) == ("thinking", {"prompt_tokens": 3})

def test_budget_charged_per_call(monkeypatch):
    # Council calls LiteLLM directly, so the loop's usage envelope never sees the
    # spend — each call must charge the run budget itself (cloud panelist can't
    # bypass max_cost_usd).
    usage = {"prompt_tokens": 100, "completion_tokens": 50,
             "prompt_tokens_details": {"cached_tokens": 20}}
    async def fake(client, base, key, model, system, user, max_tokens):
        return ("SYNTH" if "Synthesize this" in user else "POS"), dict(usage)
    monkeypatch.setattr(M, "_call", fake)
    b = Budget(max_iterations=10, max_wall_clock_s=0, max_cost_usd=10.0,
               max_total_tokens=10**9)
    cfg = {**CFG, "costs": {"m1": {"input": 1.0, "output": 2.0}}}
    ctx = ToolContext(request_id="t", config=cfg, budget=b)
    asyncio.run(CouncilDebate().execute(
        {"topic": "X", "rounds": 1, "panel": ["m1", "m2"]}, ctx))
    # 2 panelists + 1 synthesis = 3 calls charged
    assert b.tokens_prompt == 300 and b.tokens_completion == 150
    assert b.tokens_cached == 60
    # only m1 has a cost row: (80 uncached * 1.0 + 20 cached * 0.1 + 50 * 2.0) / 1e6
    assert abs(b.cost_usd - (80 * 1.0 + 20 * 0.1 + 50 * 2.0) / 1e6) < 1e-12

def test_needs_two_panelists(monkeypatch):
    async def fake(*a, **k): return "x", {}
    monkeypatch.setattr(M, "_call", fake)
    assert _run(CouncilDebate(), {"topic": "X", "panel": ["only"]}).status == "error"

def test_empty_topic():
    assert _run(CouncilDebate(), {"topic": ""}).status == "error"
