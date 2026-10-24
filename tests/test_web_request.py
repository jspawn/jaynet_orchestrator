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


# ---- 429 handling: one capped internal retry, then a hard error ----

def _429():
    return _Resp(429, {"retry-after": "0", "content-type": "application/json"},
                 [b'{"message": "Too Many Requests"}'])


def test_rate_limit_retried_once_then_succeeds(seq_http):
    seq_http.resps = [_429(), _Resp(200, {"content-type": "text/plain"}, [b"ok"])]
    r = _run({"url": "https://api.example.com/data"})
    assert r.status == "ok" and r.result["body"] == "ok"
    assert len(seq_http.calls) == 2


def test_rate_limit_still_limited_becomes_hard_error(seq_http):
    """A 429 wrapped in a tool-level ok is invisible to the loop's failure
    tracking — the model re-issues forever (live: gaia-46719c30, five
    identical Semantic Scholar calls). After the internal retry it must come
    back as a hard error that says back off."""
    seq_http.resps = [_429(), _429()]
    r = _run({"url": "https://api.example.com/data"})
    assert r.status == "error"
    assert "rate limited" in r.error and "Do NOT re-issue" in r.error
    assert len(seq_http.calls) == 2          # exactly one internal retry


def test_500_passes_through_as_payload(seq_http):
    """Only 429 is special-cased — a 500's body is often the useful part
    (API error details the model can act on)."""
    seq_http.resps = [_Resp(500, {"content-type": "application/json"},
                            [b'{"error": "no such model"}'])]
    r = _run({"url": "https://api.example.com/data"})
    assert r.status == "ok" and r.result["status_code"] == 500
    assert r.result["json"] == {"error": "no such model"}
    assert len(seq_http.calls) == 1


def test_retry_after_parsing():
    assert M._retry_after_s({"retry-after": "2"}) == 2.0
    assert M._retry_after_s({"retry-after": "3600"}) == 8.0   # capped
    assert M._retry_after_s({}) == 3.0                        # default
    assert M._retry_after_s({"retry-after": "garbage"}) == 3.0


# ---- binary responses and workspace-confined downloads ----
def _run_with_root(args, root):
    ctx = ToolContext(request_id="t", config={}, budget=None, work_root=root)
    return asyncio.run(WebRequest().execute(args, ctx))


@pytest.fixture
def download_http(fake_http, monkeypatch):
    async def public_target(host):
        return None
    monkeypatch.setattr(M, "ssrf_refusal", public_target)
    return fake_http


def test_binary_response_without_save_path_is_typed_error(download_http):
    download_http.resp = _Resp(200, {"content-type": "application/pdf"},
                               [b"%PDF-1.7\n\x00binary"])
    r = _run({"url": "https://files.example.com/document"})
    assert r.status == "error" and r.result is None
    assert r.error.startswith("binary_response:")
    assert "web.request(save_path=...)" in r.error


def test_get_save_path_downloads_into_confined_parents(download_http, tmp_path):
    payload = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n\x00binary"
    download_http.resp = _Resp(200, {"content-type": "application/pdf"},
                               [payload[:8], payload[8:]])
    r = _run_with_root({"url": "https://files.example.com/document",
                        "save_path": "downloads/nested/document.pdf"}, tmp_path)
    target = tmp_path / "downloads" / "nested" / "document.pdf"
    assert r.status == "ok"
    assert target.read_bytes() == payload
    assert r.result["path"] == str(target)
    assert r.result["bytes"] == len(payload)
    assert r.result["content_type"] == "application/pdf"
    assert "body" not in r.result


def test_save_path_escape_refused_before_request(download_http, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "escape.pdf"
    r = _run_with_root({"url": "https://files.example.com/document",
                        "save_path": "../escape.pdf"}, root)
    assert r.status == "error" and "outside your workspace" in r.error
    assert download_http.calls == []
    assert not outside.exists()


def test_save_path_refused_for_non_get(download_http, tmp_path):
    r = _run_with_root({"url": "https://files.example.com/document",
                        "method": "POST", "save_path": "document.pdf"}, tmp_path)
    assert r.status == "error" and "only supported for GET" in r.error
    assert download_http.calls == []
    assert not (tmp_path / "document.pdf").exists()


def test_oversized_download_leaves_no_partial_file(download_http, tmp_path):
    download_http.resp = _Resp(
        200, {"content-type": "application/octet-stream"},
        [b"x" * M._MAX_WIRE_BYTES, b"overflow"])
    r = _run_with_root({"url": "https://files.example.com/huge",
                        "save_path": "downloads/huge.bin"}, tmp_path)
    assert r.status == "error" and r.error.startswith("response_too_large:")
    assert not (tmp_path / "downloads" / "huge.bin").exists()
    assert not (tmp_path / "downloads").exists()
