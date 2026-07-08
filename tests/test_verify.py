"""verify.score / verify.rank — continuous logprob-expectation scoring + best-of-N."""
import asyncio
import tools.verify.score as M
from tools.verify.score import VerifyScore, VerifyRank, VerifyProbe, _expectation, _score_symbols
from runtime.tool_base import ToolContext

CFG = {"orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
       "verify": {"granularity": 20, "repeats": 1}}
def _ctx(): return ToolContext(request_id="t", config=CFG, budget=None)


# ---- pure logprob math ----
def test_symbols():
    assert _score_symbols(20) == list("ABCDEFGHIJKLMNOPQRST")

def test_expectation_extremes_and_mix():
    assert _expectation([{"token": "A", "logprob": 0.0}], 20) == 0.0          # worst
    assert _expectation([{"token": "T", "logprob": 0.0}], 20) == 1.0          # best (index 19/19)
    # 50/50 between A(0.0) and K(index 10 -> 10/19): equal mass -> mean
    import math
    mix = _expectation([{"token": "A", "logprob": math.log(0.5)},
                        {"token": "K", "logprob": math.log(0.5)}], 20)
    assert abs(mix - (0.5 * (10/19))) < 1e-9
    # space-prefixed tokenization still counts
    assert _expectation([{"token": " T", "logprob": 0.0}], 20) == 1.0

def test_expectation_none_when_no_grade():
    assert _expectation([{"token": "hello", "logprob": 0.0}], 20) is None


# ---- full score path with a mocked backend ----
class _Resp:
    def __init__(self, top): self._top = top
    def raise_for_status(self): pass
    def json(self): return {"choices": [{"logprobs": {"content": [{"top_logprobs": self._top}]}}]}
class _Client:
    def __init__(self, top): self._top = top
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def post(self, *a, **k): return _Resp(self._top)

def _mock_backend(monkeypatch, top):
    monkeypatch.setattr(M.httpx, "AsyncClient", lambda *a, **k: _Client(top))

def _run(t, args): return asyncio.run(t.execute(args, _ctx()))

def test_score_returns_continuous(monkeypatch):
    _mock_backend(monkeypatch, [{"token": "P", "logprob": 0.0}])   # P = index 15 -> 15/19
    r = _run(VerifyScore(), {"solution": "x", "task": "t", "criteria": ["c"]})
    assert r.status == "ok" and abs(r.result["score"] - 15/19) < 1e-3
    assert r.result["model"] == "local-orchestrator"                # default = brain

def test_score_model_override_and_env(monkeypatch):
    _mock_backend(monkeypatch, [{"token": "T", "logprob": 0.0}])
    assert _run(VerifyScore(), {"solution": "x", "model": "local-verifier"}).result["model"] == "local-verifier"
    monkeypatch.setenv("ORCH_VERIFIER_MODEL", "envverifier")
    assert _run(VerifyScore(), {"solution": "x"}).result["model"] == "envverifier"   # .env switch

def test_score_no_grade_errors(monkeypatch):
    _mock_backend(monkeypatch, [{"token": "nope", "logprob": 0.0}])
    assert _run(VerifyScore(), {"solution": "x"}).status == "error"


# ---- ranking (best-of-N) ----
def test_rank_orders_best_first(monkeypatch):
    async def fake(client, base, key, model, task, sol, criteria, g, k, no_think=True):
        return {"bad": 0.1, "mid": 0.5, "good": 0.9}[sol], {"c": None}
    monkeypatch.setattr(M, "_score_solution", fake)
    r = _run(VerifyRank(), {"candidates": ["bad", "good", "mid"], "task": "t"})
    assert r.status == "ok"
    assert [s["index"] for s in r.result["ranked"]] == [1, 2, 0]     # good, mid, bad
    assert r.result["best_index"] == 1 and r.result["best_score"] == 0.9

def test_rank_needs_two(monkeypatch):
    assert _run(VerifyRank(), {"candidates": ["only"]}).status == "error"


class _PClient:
    def __init__(self, data): self._d = data
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def post(self, *a, **k):
        class _R:
            def __init__(s, d): s._d = d
            def raise_for_status(s): pass
            def json(s): return s._d
        return _R(self._d)

def test_probe_grade_present(monkeypatch):
    content = [{"token": "T", "top_logprobs": [{"token": "T", "logprob": 0.0}]}]
    data = {"choices": [{"message": {"content": "", "role": "assistant"},
                         "logprobs": {"content": content}}]}
    monkeypatch.setattr(M.httpx, "AsyncClient", lambda *a, **k: _PClient(data))
    r = _run(VerifyProbe(), {"prompt": "x"})
    assert r.status == "ok" and r.result["grade_found_at_position"] == 0
    assert abs(r.result["continuous_score"] - 1.0) < 1e-6 and "OK" in r.result["verdict"]

def test_probe_reasoning_detected(monkeypatch):
    # the real failure: first token is reasoning, no grade letter
    content = [{"token": "Here", "top_logprobs": [{"token": "Here", "logprob": 0.0},
                                                  {"token": "Okay", "logprob": -2.0}]}]
    data = {"choices": [{"message": {"content": "", "role": "assistant", "reasoning_content": "Here"},
                         "logprobs": {"content": content}}]}
    monkeypatch.setattr(M.httpx, "AsyncClient", lambda *a, **k: _PClient(data))
    r = _run(VerifyProbe(), {"prompt": "x"})
    assert r.status == "ok" and r.result["grade_found_at_position"] is None
    assert "NO grade" in r.result["verdict"] and r.result["reasoning_content"] == "Here"
