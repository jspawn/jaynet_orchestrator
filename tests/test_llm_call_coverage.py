"""llm.call: alias resolution, per-alias thinking defaults, request shaping,
and content-only response parsing.

All LiteLLM HTTP is mocked (httpx.AsyncClient.post); the ctx carries no
on_token hook, so the non-streaming path is exercised.
"""
import asyncio

import httpx
import pytest

import tools.llm.cloud_models as M
from runtime.tool_base import ToolContext
from tools.llm.cloud_models import CallCloudLLM, resolve_model_alias


class _Resp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://x/v1/chat/completions")
            raise httpx.HTTPStatusError(
                "err", request=req,
                response=httpx.Response(self.status_code, text=self.text, request=req))

    def json(self):
        return self._payload


class _FakeClient:
    """Drop-in for httpx.AsyncClient; records posts, serves a canned payload."""
    posts = []
    response = None

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, headers=None):
        type(self).posts.append({"url": url, "json": json, "headers": headers})
        return type(self).response


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.posts = []
    _FakeClient.response = _Resp({
        "choices": [{"message": {"content": "ANSWER",
                                 "reasoning_content": "secret chain of thought"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 40,
                  "prompt_tokens_details": {"cached_tokens": 30}},
    })
    monkeypatch.setattr(M.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _run(args):
    ctx = ToolContext(request_id="t", config={}, budget=None)
    return asyncio.run(CallCloudLLM().execute(args, ctx))


def test_friendly_alias_request_shape_and_parsing(fake_http):
    r = _run({"model": "glm", "task": "Do the thing", "payload": "some code",
              "system": "be terse", "format": "json"})
    assert r.status == "ok"
    # only the final content is returned — never the reasoning blocks
    assert r.result == "ANSWER"
    assert r.tokens_used == {"model": "glm-5.2", "prompt": 100,
                             "completion": 40, "cached": 30}
    post = fake_http.posts[0]
    assert post["url"].endswith("/v1/chat/completions")
    assert "Authorization" in post["headers"]
    body = post["json"]
    assert body["model"] == "glm-5.2"                  # alias -> litellm name
    assert body["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "Do the thing\n\n---\n\nsome code"},
    ]
    assert body["response_format"] == {"type": "json_object"}
    # glm defaults to thinking ON: no provider-side kill switch is sent
    assert "extra_body" not in body and "reasoning_effort" not in body


def test_unknown_alias_error_lists_valid_names(fake_http):
    r = _run({"model": "banana", "task": "x"})
    assert r.status == "error"
    assert "unknown model alias 'banana'" in r.error
    assert "glm" in r.error and "qwen-plus" in r.error  # friendly + raw names
    assert fake_http.posts == []                        # no HTTP call attempted


def test_thinking_off_defaults_per_alias(fake_http):
    # qwen -> qwen-plus is in _THINKING_OFF_BY_DEFAULT: DashScope switch sent
    _run({"model": "qwen", "task": "x"})
    assert fake_http.posts[-1]["json"]["extra_body"] == {"enable_thinking": False}
    # gemini with think=False: 0-budget switch
    _run({"model": "gemini", "task": "x", "think": False})
    assert fake_http.posts[-1]["json"]["reasoning_effort"] == "none"
    # gemini default: thinking stays ON, nothing extra in the body
    _run({"model": "gemini", "task": "x"})
    body = fake_http.posts[-1]["json"]
    assert "extra_body" not in body and "reasoning_effort" not in body
    # explicit think=True overrides the qwen-plus default-off
    _run({"model": "qwen", "task": "x", "think": True})
    assert "extra_body" not in fake_http.posts[-1]["json"]


def test_http_status_error_is_caught(fake_http):
    fake_http.response = _Resp(status=429, text="rate limited")
    r = _run({"model": "qwen", "task": "x"})
    assert r.status == "error" and "HTTP 429" in r.error
    assert "rate limited" in r.error


def test_kimi_thinking_always_on(fake_http):
    # K3's reasoning is always-on at the provider: think=False sends no kill switch
    r = _run({"model": "kimi", "task": "x", "think": False})
    assert r.status == "ok"
    body = fake_http.posts[-1]["json"]
    assert body["model"] == "kimi-k3"
    assert "extra_body" not in body and "reasoning_effort" not in body


def test_resolve_model_alias_tolerance():
    # case and underscore/dash differences normalize to the litellm alias
    assert resolve_model_alias("kimi") == "kimi-k3"
    assert resolve_model_alias("Kimi-K3") == "kimi-k3"
    assert resolve_model_alias("GLM") == "glm-5.2"
    assert resolve_model_alias("Qwen_Plus") == "qwen-plus"
    assert resolve_model_alias("Gemini-Pro") == "gemini-pro"
    assert resolve_model_alias("LOCAL_SPECIALIST") == "local-specialist"
    assert resolve_model_alias("glm") == "glm-5.2"
    for bad in ("gpt-4", "", None):
        assert resolve_model_alias(bad) is None
