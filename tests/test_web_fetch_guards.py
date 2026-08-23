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
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
    "http://100.100.2.136/",             # Alibaba metadata (CGNAT)
])
def test_loopback_urls_rejected_before_connecting(url, monkeypatch):
    def _no_network(*a, **k):
        raise AssertionError("must not connect")
    monkeypatch.setattr(M.httpx, "AsyncClient", _no_network)
    r = _run(url)
    assert r.status == "error" and "refuses" in r.error


def test_hostname_resolving_to_loopback_rejected(monkeypatch):
    """A name that resolves to a blocked address is as refused as the literal."""
    def _no_network(*a, **k):
        raise AssertionError("must not connect")
    monkeypatch.setattr(M.httpx, "AsyncClient", _no_network)

    async def fake_resolve(host):
        return ["127.0.0.1"]
    monkeypatch.setattr(M, "_resolve_ips", fake_resolve)
    r = _run("http://internal.example:4000/keys")
    assert r.status == "error" and "loopback" in r.error


def test_unresolvable_hostname_passes_guard(monkeypatch):
    """gaierror must not block the fetch (offline test env; connect fails on its
    own in production)."""
    import socket as _socket

    async def fail_resolve(host):
        raise _socket.gaierror("no DNS")
    monkeypatch.setattr(M, "_resolve_ips", fail_resolve)
    _stub_transport(monkeypatch, _Resp([b"<html><body>hello world</body></html>"]))
    r = _run("https://no-such-host.invalid/page")
    assert r.status == "ok" and "hello world" in r.result["content"]


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


# ---- redirect hops are re-validated ----
class _RedirectResp(_Resp):
    is_redirect = True

    def __init__(self, location):
        super().__init__([])
        self.headers = {"location": location}


def test_redirect_to_loopback_refused(monkeypatch):
    """A public URL must not be able to 302 the fetch into loopback."""
    async def fake_resolve(host):
        return ["93.184.216.34"]          # public IP for the first hop
    monkeypatch.setattr(M, "_resolve_ips", fake_resolve)

    calls = []

    class CountingClient(_Client):
        def stream(self, method, url):
            calls.append(url)
            return _Stream(self._resp)

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(M.httpx, "AsyncClient",
                        lambda *a, **k: CountingClient(_RedirectResp("http://127.0.0.1:4000/x")))
    r = _run("https://example.com/start")
    assert r.status == "error" and "loopback" in r.error
    assert calls == ["https://example.com/start"]   # second hop never requested


# ---- HTTP status errors carry recovery hints ----
class _StatusResp(_Resp):
    def __init__(self, code):
        super().__init__([])
        self._code = code

    def raise_for_status(self):
        import httpx as _httpx
        req = _httpx.Request("GET", "https://example.com/x")
        raise _httpx.HTTPStatusError(
            f"{self._code}", request=req,
            response=_httpx.Response(self._code, request=req))


def test_blocked_status_suggests_render(monkeypatch):
    _stub_transport(monkeypatch, _StatusResp(406))
    r = _run("https://example.com/waf-fronted")
    assert r.status == "error" and "HTTP 406" in r.error
    assert "web.render" in r.error


def test_not_found_status_discourages_url_guessing(monkeypatch):
    _stub_transport(monkeypatch, _StatusResp(404))
    r = _run("https://example.com/guessed-path")
    assert r.status == "error" and "HTTP 404" in r.error
    assert "don't guess URL variants" in r.error


def test_unmapped_status_stays_plain(monkeypatch):
    _stub_transport(monkeypatch, _StatusResp(500))
    r = _run("https://example.com/broken")
    assert r.status == "error" and r.error.endswith("HTTP 500 for https://example.com/broken")


# ---- thin-content hint (success that is really a JS shell) ----
def test_thin_content_suggests_render(monkeypatch):
    _stub_transport(monkeypatch, _Resp([b"<html><body>loading...</body></html>"]))
    r = _run("https://example.com/spa")
    assert r.status == "ok"
    assert "web.render" in r.result["hint"]


def test_full_content_carries_no_hint(monkeypatch):
    body = b"<html><body>" + b"real article text " * 100 + b"</body></html>"
    _stub_transport(monkeypatch, _Resp([body]))
    r = _run("https://example.com/article")
    assert r.status == "ok"
    assert len(r.result["content"]) >= M._THIN_CONTENT_CHARS
    assert "hint" not in r.result


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
