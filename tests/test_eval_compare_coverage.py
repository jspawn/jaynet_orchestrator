"""eval.compare: alias resolution, per-model results, summary fields, and
run-budget charging of the direct-to-LiteLLM sub-calls.

The LiteLLM round-trip is mocked (httpx.AsyncClient.post); a real
runtime.budget.Budget asserts the per-model spend the loop's tokens_used
envelope never sees (mirrors the council charging test).
"""
import asyncio

import httpx
import pytest

import tools.eval.compare as M
from tools.eval.compare import EvalCompare, _resolve
from runtime.budget import Budget
from runtime.tool_base import ToolContext

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "costs": {
        "glm-5.2": {"input": 1.0, "output": 2.0},
        "qwen-plus": {"input": 0.5, "output": 1.0},
    },
}


class _Resp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://x:4000/v1/chat/completions")
            raise httpx.HTTPStatusError(
                "err", request=req,
                response=httpx.Response(self.status_code, text=self.text, request=req))

    def json(self):
        return self._payload


class _FakeClient:
    """Drop-in for httpx.AsyncClient; routes each post to a per-model handler."""
    posts = []
    handler = None                     # async callable(model_name) -> _Resp

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, headers=None):
        type(self).posts.append({"url": url, "json": json, "headers": headers})
        return await type(self).handler(json["model"])


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.posts = []
    _FakeClient.handler = None
    monkeypatch.setattr(M.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _payload(content, prompt=100, completion=50, cached=0):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cached:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return _Resp({"choices": [{"message": {"content": content}}], "usage": usage})


def _run(args, ctx=None):
    return asyncio.run(EvalCompare().execute(
        args, ctx or ToolContext(request_id="t", config=CFG, budget=None)))


def test_alias_resolution_and_request_bodies(fake_http):
    async def handler(model):
        return _payload(f"OUT[{model}]", prompt=10, completion=5)
    fake_http.handler = handler
    r = _run({"prompt": "P", "models": ["glm", "qwen-plus", "local"],
              "system": "S", "json": True})
    assert r.status == "ok"
    # gather preserves input order; each result carries both forms of the name
    got = [(x["model"], x["model_name"]) for x in r.result["results"]]
    assert got == [("glm", "glm-5.2"),           # friendly alias via _MODEL_MAP
                   ("qwen-plus", "qwen-plus"),   # raw LiteLLM name passes through
                   ("local", "local-orchestrator")]  # local -> orchestrator.model
    assert all(x["status"] == "ok" for x in r.result["results"])
    assert r.result["results"][0]["output"] == "OUT[glm-5.2]"
    # request shape: shared system + user prompt, json mode, target URL, auth header
    bodies = {p["json"]["model"]: p for p in fake_http.posts}
    assert all(p["url"] == "http://x:4000/v1/chat/completions" for p in fake_http.posts)
    assert all("Authorization" in p["headers"] for p in fake_http.posts)
    glm_body = bodies["glm-5.2"]["json"]
    assert glm_body["messages"] == [{"role": "system", "content": "S"},
                                    {"role": "user", "content": "P"}]
    assert glm_body["response_format"] == {"type": "json_object"}
    assert glm_body["temperature"] == 0.3
    assert "max_tokens" not in glm_body        # omitted unless requested


def test_summary_fastest_cheapest(fake_http):
    async def handler(model):
        if model == "glm-5.2":
            await asyncio.sleep(0.05)          # glm answers slowly
            return _payload("slow", prompt=100, completion=50)
        return _payload("fast", prompt=10, completion=5)
    fake_http.handler = handler
    r = _run({"prompt": "P", "models": ["glm", "qwen"]})
    s = r.result["summary"]
    assert s["succeeded"] == 2 and s["failed"] == 0
    assert s["fastest"] == "qwen"              # 50ms vs ~0ms
    # glm-5.2: (100*1.0 + 50*2.0)/1e6 = 200e-6 ; qwen-plus: (10*0.5 + 5*1.0)/1e6 = 10e-6
    assert s["cheapest"] == "qwen"
    assert abs(s["total_cost_usd"] - 210e-6) < 1e-12
    assert abs(r.cost_usd - 210e-6) < 1e-12    # envelope mirrors the total


def test_budget_charged_per_model(fake_http):
    # eval.compare calls LiteLLM directly, so each sub-call must charge the run
    # budget itself — a pricey multi-model compare can't bypass max_cost_usd.
    async def handler(model):
        if model == "glm-5.2":
            return _payload("a", prompt=100, completion=50, cached=20)
        return _payload("b", prompt=10, completion=5)
    fake_http.handler = handler
    b = Budget(max_iterations=10, max_wall_clock_s=0, max_cost_usd=10.0,
               max_total_tokens=10**9)
    ctx = ToolContext(request_id="t", config=CFG, budget=b)
    _run({"prompt": "P", "models": ["glm", "qwen"]}, ctx)
    assert b.tokens_prompt == 110 and b.tokens_completion == 55
    assert b.tokens_cached == 20
    # glm-5.2: (80 uncached * 1.0 + 20 cached * 0.1 + 50 * 2.0) / 1e6 = 182e-6
    # qwen-plus: (10 * 0.5 + 5 * 1.0) / 1e6 = 10e-6
    assert abs(b.cost_usd - 192e-6) < 1e-12


def test_http_error_is_per_model_not_fatal(fake_http):
    async def handler(model):
        if model == "glm-5.2":
            return _Resp(status=500, text="backend exploded")
        return _payload("fine", prompt=10, completion=5)
    fake_http.handler = handler
    r = _run({"prompt": "P", "models": ["glm", "qwen"]})
    assert r.status == "ok"                    # the compare itself still succeeds
    bad, good = r.result["results"]
    assert bad["status"] == "error" and "HTTP 500" in bad["error"]
    assert "backend exploded" in bad["error"]
    assert good["status"] == "ok"
    s = r.result["summary"]
    assert (s["succeeded"], s["failed"]) == (1, 1)
    assert s["fastest"] == "qwen" and s["cheapest"] == "qwen"


def test_network_exception_is_per_model(fake_http):
    async def handler(model):
        raise httpx.ConnectError("no route")
    fake_http.handler = handler
    r = _run({"prompt": "P", "models": ["glm"]})
    res = r.result["results"][0]
    assert res["status"] == "error" and "ConnectError" in res["error"]
    assert r.result["summary"]["failed"] == 1
    assert r.result["summary"]["fastest"] is None


def test_no_models_is_error():
    r = _run({"prompt": "P", "models": []})
    assert r.status == "error" and "no models" in r.error


def test_resolve_helper():
    ctx = ToolContext(request_id="t", config=CFG, budget=None)
    assert _resolve("glm", ctx) == "glm-5.2"
    assert _resolve("local", ctx) == "local-orchestrator"
    assert _resolve("orchestrator", ctx) == "local-orchestrator"
    assert _resolve("some-raw-name", ctx) == "some-raw-name"
