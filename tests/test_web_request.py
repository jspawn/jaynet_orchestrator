"""web.request: method gating, loopback refusal, body rules, response parsing.

All HTTP is mocked (httpx.AsyncClient.stream as an async CM over canned chunks).
"""
import asyncio

import pytest

import tools.web.request as M
from runtime.tool_base import ToolContext
from tools.web.request import WebRequest


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


class _RedirectResp(_Resp):
    is_redirect = True


def test_redirect_to_loopback_refused(fake_http, monkeypatch):
    """A public endpoint must not 302 the request into loopback."""
    async def fake_resolve(host):
        return ["93.184.216.34"]
    import tools.web.search_fetch as sf
    monkeypatch.setattr(sf, "_resolve_ips", fake_resolve)
    fake_http.resp = _RedirectResp(302, {"location": "http://127.0.0.1:4000/x"})
    r = _run({"url": "https://api.example.com/start"})
    assert r.status == "error" and "loopback" in r.error
    assert len(fake_http.calls) == 1          # second hop never requested


class _SeqClient:
    """Serves a scripted sequence of responses, one per request."""
    calls = []
    resps = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        type(self).calls.append({"method": method, "url": url, **kw})
        return _StreamCM(type(self).resps.pop(0))


@pytest.fixture
def seq_http(monkeypatch):
    _SeqClient.calls = []
    _SeqClient.resps = []
    monkeypatch.setattr(M.httpx, "AsyncClient", _SeqClient)

    async def fake_resolve(host):
        return ["93.184.216.34"]
    import tools.web.search_fetch as sf
    monkeypatch.setattr(sf, "_resolve_ips", fake_resolve)
    return _SeqClient


def test_cross_origin_redirect_drops_credentials(seq_http):
    """Authorization/Cookie must not be replayed to a different host."""
    seq_http.resps = [
        _RedirectResp(302, {"location": "https://other.example.com/land"}),
        _Resp(200, {"content-type": "text/plain"}, [b"ok"]),
    ]
    r = _run({"url": "https://api.example.com/start",
              "headers": {"Authorization": "Bearer k", "Cookie": "s=1",
                          "X-Custom": "keep"}})
    assert r.status == "ok" and r.result["body"] == "ok"
    first, second = seq_http.calls
    assert first["headers"]["Authorization"] == "Bearer k"
    assert "Authorization" not in second["headers"]
    assert "Cookie" not in second["headers"]
    assert second["headers"]["X-Custom"] == "keep"    # non-credential headers stay
    assert second["headers"]["User-Agent"]


def test_cross_scheme_redirect_drops_credentials(seq_http):
    """https -> http on the same hostname is a different origin too."""
    seq_http.resps = [
        _RedirectResp(302, {"location": "http://api.example.com/plain"}),
        _Resp(200, {"content-type": "text/plain"}, [b"ok"]),
    ]
    r = _run({"url": "https://api.example.com/start",
              "headers": {"Authorization": "Bearer k"}})
    assert r.status == "ok"
    assert "Authorization" not in seq_http.calls[1]["headers"]


def test_same_origin_redirect_keeps_credentials(seq_http):
    seq_http.resps = [
        _RedirectResp(302, {"location": "/next"}),
        _Resp(200, {"content-type": "text/plain"}, [b"ok"]),
    ]
    r = _run({"url": "https://api.example.com/start",
              "headers": {"Authorization": "Bearer k"}})
    assert r.status == "ok"
    second = seq_http.calls[1]
    assert second["url"] == "https://api.example.com/next"
    assert second["headers"]["Authorization"] == "Bearer k"
