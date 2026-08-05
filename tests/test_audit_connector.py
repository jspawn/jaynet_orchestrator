"""Audit regressions for connectors (S11).

Path-param values are URL-quoted so '?', '#', '/' can never reshape the
request path or smuggle in a query string; responses are streamed with a hard
byte cap and REJECTED (not truncated) when they exceed it. No network: httpx
is monkeypatched with streaming-capable fakes.
"""
import pytest
import yaml

from runtime.tool_base import ToolContext
from tools.connector import _MAX_RESPONSE_BYTES, load_connectors

from conftest import run

BASE = {
    "name": "custom.geo",
    "base_url": "https://api.example.ch",
    "request": {"method": "GET", "path": "/v1/city/{city}"},
    "params": {"city": {"type": "string", "required": True}},
}


def _ctx():
    return ToolContext(request_id="t", config={}, budget=None)


def _tool(tmp_path, doc=BASE):
    (tmp_path / "geo.yaml").write_text(yaml.safe_dump(doc))
    return load_connectors(tmp_path)[0]


class _StreamResponse:
    def __init__(self, status, chunks):
        self.status_code = status
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        gen = getattr(self, "_gen", None)
        if gen is not None:
            await gen.aclose()               # httpx closes the stream on exit
        return False

    def aiter_bytes(self):
        async def gen():
            for c in self._chunks:
                yield c
        self._gen = gen()
        return self._gen


class _StreamClient:
    """httpx.AsyncClient stand-in with the streaming API (client.stream)."""
    calls: list = []
    next_status = 200
    next_chunks: list = [b"BODY"]

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return _StreamResponse(self.next_status, self.next_chunks)


@pytest.fixture
def fake_http(monkeypatch):
    _StreamClient.calls = []
    _StreamClient.next_status = 200
    _StreamClient.next_chunks = [b"BODY"]
    monkeypatch.setattr("tools.connector.httpx.AsyncClient", _StreamClient)
    return _StreamClient


def test_path_param_is_url_quoted(tmp_path, fake_http):
    t = _tool(tmp_path)
    res = run(t.execute({"city": "a/b?x=1#f"}, _ctx()))
    assert res.status == "ok"
    url = fake_http.calls[0]["url"]
    # The whole value stays inside its path segment — no query, no fragment.
    assert url == "https://api.example.ch/v1/city/a%2Fb%3Fx%3D1%23f"


def test_query_params_pass_through_for_httpx_to_encode(tmp_path, fake_http):
    doc = dict(BASE, request={"method": "GET", "path": "/v1/search"},
               params={"q": {"type": "string", "required": True}})
    t = _tool(tmp_path, doc)
    res = run(t.execute({"q": "a&b=c"}, _ctx()))
    assert res.status == "ok"
    call = fake_http.calls[0]
    assert call["url"] == "https://api.example.ch/v1/search"
    assert call["params"] == {"q": "a&b=c"}      # httpx encodes, we don't


def test_oversize_streamed_response_rejected_early(tmp_path, fake_http):
    pulled = []

    def big():
        for i in range(4):
            pulled.append(i)
            yield b"x" * (4 * 1024 * 1024)       # 16 MB total, cap is 8 MB

    fake_http.next_chunks = big()
    t = _tool(tmp_path)
    res = run(t.execute({"city": "x"}, _ctx()))
    assert res.status == "error"
    assert "8 MB cap" in res.error
    # Reading stopped at the cap — the 4th chunk was never pulled off the wire.
    assert pulled == [0, 1, 2]


def test_exactly_capped_response_is_ok(tmp_path, fake_http):
    fake_http.next_chunks = [b"x" * _MAX_RESPONSE_BYTES]
    t = _tool(tmp_path)
    res = run(t.execute({"city": "x"}, _ctx()))
    assert res.status == "ok"


def test_oversize_buffered_response_rejected(tmp_path, monkeypatch):
    """The buffered fallback path (test doubles without .stream) caps too."""
    class Resp:
        status_code = 200
        text = "x" * (_MAX_RESPONSE_BYTES + 1)

    class Client:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            return Resp()

    monkeypatch.setattr("tools.connector.httpx.AsyncClient", Client)
    t = _tool(tmp_path)
    res = run(t.execute({"city": "x"}, _ctx()))
    assert res.status == "error"
    assert "8 MB cap" in res.error
