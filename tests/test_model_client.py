"""Retry-on-500 for llama.cpp's server-side tool-call JSON parse failures.

The backend discards the whole generation when a long tool-call argument
isn't valid JSON (unterminated string in a multi-KB fs.write payload) and
answers HTTP 500. One nudged retry saves the run; anything else, and a
second failure, stays a hard error (live: 5/118 eval cases died to this).
"""
import asyncio

import pytest

from runtime.model_client import ModelClientMixin, _is_toolcall_json_500

_LLAMACPP_500 = ('{"error":{"message":"litellm.InternalServerError: '
                 'OpenAIException - Failed to parse tool call arguments as '
                 'JSON: [json.exception.parse_error.101] parse error: '
                 'invalid string: missing closing quote"}}')


class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


def _ok(content="done"):
    return _Resp(200, payload={
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"total_tokens": 3}})


class _FakeRT(ModelClientMixin):
    def __init__(self, responses):
        self.config = {}
        self.model = "local-orchestrator"
        self.litellm_base = "http://litellm"
        self._local_concurrency = {}
        self._model_sems = {}
        self._local_aliases = frozenset()
        self._responses = list(responses)
        self.posts = []

    def _auth_headers(self):
        return {}

    def _http_client(self):
        host = self

        class _C:
            async def post(self, url, json=None, headers=None, timeout=None):
                host.posts.append(json)
                return host._responses.pop(0)

        return _C()


def _turn(rt, messages):
    return asyncio.run(rt._model_turn(messages, []))


def test_toolcall_json_500_retries_once_with_nudge():
    rt = _FakeRT([_Resp(500, _LLAMACPP_500), _ok()])
    messages = [{"role": "user", "content": "write the file"}]
    out = _turn(rt, messages)
    assert out["message"]["content"] == "done"
    assert len(rt.posts) == 2
    retry_msgs = rt.posts[1]["messages"]
    assert retry_msgs[-1]["role"] == "user"
    assert "not valid JSON" in retry_msgs[-1]["content"]
    assert "smaller fs.write" in retry_msgs[-1]["content"]
    # the caller's history is not polluted with the nudge
    assert messages == [{"role": "user", "content": "write the file"}]


def test_toolcall_json_500_twice_is_a_hard_error():
    rt = _FakeRT([_Resp(500, _LLAMACPP_500), _Resp(500, _LLAMACPP_500)])
    with pytest.raises(RuntimeError, match="LiteLLM 500"):
        _turn(rt, [{"role": "user", "content": "hi"}])
    assert len(rt.posts) == 2   # exactly one retry, never a loop


def test_other_500s_do_not_retry():
    rt = _FakeRT([_Resp(500, '{"error":{"message":"backend exploded"}}')])
    with pytest.raises(RuntimeError, match="LiteLLM 500"):
        _turn(rt, [{"role": "user", "content": "hi"}])
    assert len(rt.posts) == 1


def test_detector_is_specific():
    assert _is_toolcall_json_500(500, _LLAMACPP_500)
    assert not _is_toolcall_json_500(408, _LLAMACPP_500)      # timeout ≠ parse
    assert not _is_toolcall_json_500(500, "model not found")
    assert not _is_toolcall_json_500(429, "tool call parse rate limit")
