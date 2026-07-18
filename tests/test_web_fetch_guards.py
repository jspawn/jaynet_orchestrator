"""web.fetch direct-GET guards: loopback/SSRF targets are rejected before any
connection, and the response body is read under a hard byte cap instead of
being slurped fully into memory. All httpx traffic is stubbed — no network."""
import asyncio
import pytest

import tools.web.search_fetch as M
from tools.web.search_fetch import WebFetch


class _Ctx:
    config = {}


def _run(url):
    return asyncio.run(WebFetch().execute({"url": url}, _Ctx()))


class _Resp:
    """Stands in for a streamed httpx response."""
    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        pass

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _Stream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _Client:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url):
        return _Stream(self._resp)


def _stub_transport(monkeypatch, resp):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)   # force the direct path
    monkeypatch.setattr(M.httpx, "AsyncClient", lambda *a, **k: _Client(resp))


# ---- loopback SSRF guard ----
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:4000/v1/models",   # the LiteLLM admin API
    "http://127.1.2.3/x",                # rest of 127.0.0.0/8
    "http://localhost:4000/keys",
    "http://0.0.0.0:4000/",
    "http://[::1]:4000/",
])
def test_loopback_urls_rejected_before_connecting(url, monkeypatch):
    def _no_network(*a, **k):
        raise AssertionError("must not connect")
    monkeypatch.setattr(M.httpx, "AsyncClient", _no_network)
    r = _run(url)
    assert r.status == "error" and "loopback" in r.error


@pytest.mark.parametrize("url", [
    "https://example.com/page",          # normal web URL
    "http://192.168.1.20:8080/status",   # RFC1918 LAN stays fetchable
    "http://10.0.0.5/health",
])
def test_normal_and_lan_urls_still_fetch(url, monkeypatch):
    _stub_transport(monkeypatch, _Resp([b"<html><body>hello world</body></html>"]))
    r = _run(url)
    assert r.status == "ok" and r.result["via"] == "direct"
    assert "hello world" in r.result["content"]


# ---- hard byte cap ----
def test_body_capped_at_max_bytes(monkeypatch):
    # 20 x 1 MiB on the wire; the reader must stop right past the cap, not
    # consume the whole body.
    consumed = []

    class CountingResp(_Resp):
        async def aiter_bytes(self):
            for c in self._chunks:
                consumed.append(len(c))
                yield c

    _stub_transport(monkeypatch, CountingResp([b"x" * (1 << 20)] * 20))
    r = _run("https://example.com/huge")
    assert r.status == "ok"
    assert sum(consumed) <= M._MAX_FETCH_BYTES + (1 << 20)
    assert r.result["original_length"] <= M._MAX_FETCH_BYTES
    assert r.result["truncated"] is True
