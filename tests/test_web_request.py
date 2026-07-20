"""web.request: method gating, loopback refusal, body rules, response parsing.

All HTTP is mocked (httpx.AsyncClient.stream as an async CM over canned chunks).
"""
import asyncio

import pytest

import tools.web.request as M
from tools.web.request import WebRequest
from runtime.tool_base import ToolContext


class _Resp:
    def __init__(self, status=200, headers=None, chunks=(b"data",)):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _StreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    calls = []
    resp = _Resp()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        type(self).calls.append({"method": method, "url": url, **kw})
        return _StreamCM(type(self).resp)


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.resp = _Resp()
    monkeypatch.setattr(M.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _ctx():
    return ToolContext(request_id="t", config={}, budget=None)


def _run(args):
    return asyncio.run(WebRequest().execute(args, _ctx()))


def test_dynamic_confirmation():
    t = WebRequest()
    assert t.needs_confirmation({"method": "GET"}, None) is False
    assert t.needs_confirmation({"method": "HEAD"}, None) is False
    assert t.needs_confirmation({"method": "OPTIONS"}, None) is False
    assert t.needs_confirmation({"method": "POST"}, None) is True
    assert t.needs_confirmation({"method": "PUT"}, None) is True
    assert t.needs_confirmation({"method": "DELETE"}, None) is True
    assert t.needs_confirmation({}, None) is False       # default GET


def test_loopback_refused():
    for u in ("http://127.0.0.1:4000/x", "http://localhost:8090/health"):
        r = _run({"url": u})
        assert r.status == "error" and "loopback" in r.error


def test_bad_scheme_and_method():
    assert "scheme" in _run({"url": "ftp://x"}).error
    assert "method" in _run({"url": "https://x", "method": "TRACE"}).error.lower()


def test_get_sends_no_body(fake_http):
    r = _run({"url": "https://api.example.com/items"})
    assert r.status == "ok" and r.result["status_code"] == 200
    call = fake_http.calls[0]
    assert call["method"] == "GET"
    assert "json" not in call and "content" not in call


def test_post_json_body_and_headers(fake_http):
    fake_http.resp = _Resp(201, {"content-type": "application/json"}, [b'{"id": 7}'])
    r = _run({"url": "https://api.example.com/items", "method": "POST",
              "json": {"name": "n"}, "headers": {"Authorization": "Bearer k"}})
    assert r.status == "ok" and r.result["json"] == {"id": 7}
    call = fake_http.calls[0]
    assert call["json"] == {"name": "n"}
    assert call["headers"]["Authorization"] == "Bearer k"
    assert call["headers"]["User-Agent"]


def test_read_method_with_body_refused():
    r = _run({"url": "https://x", "method": "GET", "body": "nope"})
    assert r.status == "error" and "body" in r.error


def test_text_response_truncated(fake_http):
    fake_http.resp = _Resp(200, {"content-type": "text/plain"}, [b"x" * 5000])
    r = _run({"url": "https://x", "max_chars": 1000})
    assert len(r.result["body"]) == 1000 and r.result["truncated"] is True


def test_error_status_is_a_result_not_an_exception(fake_http):
    # transport succeeded; the status code is the answer (no raise_for_status)
    fake_http.resp = _Resp(404, {"content-type": "text/plain"}, [b"missing"])
    r = _run({"url": "https://x/none"})
    assert r.status == "ok" and r.result["status_code"] == 404
    assert r.result["body"] == "missing"
