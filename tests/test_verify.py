"""verify.* — numeric 0-9 scale, dominant-position extraction, probe, ranking."""
import asyncio
import math
import tools.verify.score as M
from tools.verify.score import (VerifyScore, VerifyRank, VerifyProbe,
                                 _expectation, _scale_symbols)
from runtime.tool_base import ToolContext

CFG = {"orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
       "verify": {"scale": "numeric", "repeats": 1, "min_grade_mass": 0.5}}
def _ctx(): return ToolContext(request_id="t", config=CFG, budget=None)
def _lp(p): return math.log(max(p, 1e-12))


# ---- scale ----
def test_numeric_scale():
    syms, values = _scale_symbols("numeric", 20)
    assert syms == list("0123456789")
    assert values["0"] == 0.0 and values["9"] == 1.0 and abs(values["7"] - 7/9) < 1e-9

def test_letters_scale_still_available():
    syms, values = _scale_symbols("letters", 20)
    assert syms[0] == "A" and syms[-1] == "T" and values["A"] == 0.0 and values["T"] == 1.0


# ---- dominant-position extraction (the bug fix) ----
def test_expectation_dominant_digit():
    _, values = _scale_symbols("numeric", 10)
    assert abs(_expectation([{"token": "7", "logprob": 0.0}], values) - 7/9) < 1e-9
    # 60/40 between 8 and 6 -> weighted mean, still dominant
    mix = _expectation([{"token": "8", "logprob": _lp(0.6)}, {"token": "6", "logprob": _lp(0.4)}], values)
    assert abs(mix - (0.6*(8/9) + 0.4*(6/9))) < 1e-9

def test_expectation_rejects_trace_token():
    # THE regression: '**'@0.66 dominates, a stray 'I' (letters) / '1' with tiny mass must NOT count
    _, values = _scale_symbols("numeric", 10)
    top = [{"token": "**", "logprob": _lp(0.66)}, {"token": "The", "logprob": _lp(0.33)},
           {"token": "1", "logprob": _lp(0.0004)}]
    assert _expectation(top, values, min_mass=0.5) is None      # grade mass 0.0004 « 0.5 -> not the grade

def test_expectation_none_when_no_grade():
    _, values = _scale_symbols("numeric", 10)
    assert _expectation([{"token": "hello", "logprob": 0.0}], values) is None


# ---- full score path with a mocked backend ----
class _Resp:
    def __init__(self, content): self._c = content
    def raise_for_status(self): pass
    def json(self): return {"choices": [{"message": {"content": ""}, "logprobs": {"content": self._c}}]}
class _Client:
    def __init__(self, content): self._c = content
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def post(self, *a, **k): return _Resp(self._c)

def _mock(monkeypatch, content):
    monkeypatch.setattr(M.httpx, "AsyncClient", lambda *a, **k: _Client(content))

def _run(t, args): return asyncio.run(t.execute(args, _ctx()))

def test_score_returns_continuous(monkeypatch):
    _mock(monkeypatch, [{"top_logprobs": [{"token": "7", "logprob": 0.0}]}])   # grade "7" -> 7/9
    r = _run(VerifyScore(), {"solution": "x", "task": "t", "criteria": ["c"]})
    assert r.status == "ok" and abs(r.result["score"] - 7/9) < 1e-3
    assert r.result["model"] == "local-orchestrator" and r.result["scale"] == "numeric"

def test_score_skips_markdown_then_grades(monkeypatch):
    # realistic: **, Grade, :, then the digit — only the digit position is dominant
    _mock(monkeypatch, [
        {"top_logprobs": [{"token": "**", "logprob": _lp(0.66)}, {"token": "1", "logprob": _lp(0.0004)}]},
        {"top_logprobs": [{"token": "Grade", "logprob": _lp(0.99)}]},
        {"top_logprobs": [{"token": " 9", "logprob": _lp(0.95)}]},   # dominant grade -> 9 -> 1.0
    ])
    r = _run(VerifyScore(), {"solution": "def add(a,b): return a+b"})
    assert r.status == "ok" and abs(r.result["score"] - 1.0) < 1e-3   # sane: good code -> top score

def test_score_model_override_and_env(monkeypatch):
    _mock(monkeypatch, [{"top_logprobs": [{"token": "5", "logprob": 0.0}]}])
    assert _run(VerifyScore(), {"solution": "x", "model": "local-verifier"}).result["model"] == "local-verifier"
    monkeypatch.setenv("ORCH_VERIFIER_MODEL", "envverifier")
    assert _run(VerifyScore(), {"solution": "x"}).result["model"] == "envverifier"

def test_score_no_grade_errors(monkeypatch):
    _mock(monkeypatch, [{"top_logprobs": [{"token": "nope", "logprob": 0.0}]}])
    assert _run(VerifyScore(), {"solution": "x"}).status == "error"


# ---- ranking ----
def test_rank_orders_best_first(monkeypatch):
    async def fake(client, base, key, model, task, sol, criteria, syms, values, k, no_think=True, min_mass=0.5, constrain=True):
        return {"bad": 0.1, "mid": 0.5, "good": 0.9}[sol], {"c": None}
    monkeypatch.setattr(M, "_score_solution", fake)
    r = _run(VerifyRank(), {"candidates": ["bad", "good", "mid"], "task": "t"})
    assert [s["index"] for s in r.result["ranked"]] == [1, 2, 0]
    assert r.result["best_index"] == 1 and r.result["best_score"] == 0.9

def test_rank_needs_two(monkeypatch):
    assert _run(VerifyRank(), {"candidates": ["only"]}).status == "error"


# ---- probe ----
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

def test_probe_dominant_grade(monkeypatch):
    content = [{"token": " 8", "top_logprobs": [{"token": " 8", "logprob": 0.0}]}]
    data = {"choices": [{"message": {"content": ""}, "logprobs": {"content": content}}]}
    monkeypatch.setattr(M.httpx, "AsyncClient", lambda *a, **k: _PClient(data))
    r = _run(VerifyProbe(), {"prompt": "x"})
    assert r.result["grade_found_at_position"] == 0 and "OK" in r.result["verdict"]
    assert abs(r.result["continuous_score"] - 8/9) < 1e-3

def test_probe_rejects_your_false_positive(monkeypatch):
    # exactly your trace: '**' dominates pos 0, a stray letter/digit at p=0.0004
    content = [{"token": "**", "top_logprobs": [
        {"token": "**", "logprob": _lp(0.6688)}, {"token": "The", "logprob": _lp(0.2667)},
        {"token": "1", "logprob": _lp(0.0004)}]}]
    data = {"choices": [{"message": {"content": ""}, "logprobs": {"content": content}}]}
    monkeypatch.setattr(M.httpx, "AsyncClient", lambda *a, **k: _PClient(data))
    r = _run(VerifyProbe(), {"prompt": "x"})
    assert r.result["grade_found_at_position"] is None      # no longer a false positive
    assert "NO grade" in r.result["verdict"] and r.result["continuous_score"] is None


def test_grammar_fallback_uses_emitted_digit(monkeypatch):
    # constrain=True: grammar forces a digit, but backend returns RAW (chatty) logprobs
    # where the digit isn't dominant. The emitted token IS the grade -> use it.
    content = [{"token": " 7", "top_logprobs": [
        {"token": "To", "logprob": _lp(0.7)}, {"token": " 7", "logprob": _lp(0.05)}]}]
    data = {"choices": [{"message": {"content": ""}, "logprobs": {"content": content}}]}
    monkeypatch.setattr(M.httpx, "AsyncClient", lambda *a, **k: _PClient(data))
    r = _run(VerifyProbe(), {"prompt": "x"})           # constrain defaults true
    assert r.result["grade_found_at_position"] == 0
    assert abs(r.result["continuous_score"] - 7/9) < 1e-3 and r.result["constrain"] is True

def test_no_constrain_rejects_nondominant(monkeypatch):
    # constrain=False: same raw response, NO grammar guarantee -> the stray digit is NOT trusted
    content = [{"token": "To", "top_logprobs": [
        {"token": "To", "logprob": _lp(0.7)}, {"token": " 7", "logprob": _lp(0.05)}]}]
    data = {"choices": [{"message": {"content": ""}, "logprobs": {"content": content}}]}
    monkeypatch.setattr(M.httpx, "AsyncClient", lambda *a, **k: _PClient(data))
    r = _run(VerifyProbe(), {"prompt": "x", "constrain": False})
    assert r.result["grade_found_at_position"] is None    # no false positive without the grammar
