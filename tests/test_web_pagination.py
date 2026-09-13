"""Offset pagination for web.fetch / web.request / web.render.

A truncated page used to be a dead end: the model saw `truncated: true` but
had no way to read past the cap, so it refetched the same head with shrinking
max_chars (live: gaia-d0633230). Now every truncation carries an actionable
hint ("call again with offset=N") and the tools honour that offset.
"""
import asyncio

import tools.web.render as R
import tools.web.request as Q
from runtime.tool_base import ToolContext
from tools.web import search_fetch as sf

# Position-identifying content: char at index N is str(N % 10), so a slice's
# first char proves where it was taken from.
TEXT = "".join(str(i % 10) for i in range(60000))


def test_page_helper():
    chunk, truncated, hint = sf._page(TEXT, 1000, 0)
    assert chunk == TEXT[:1000]
    assert truncated is True
    assert hint == ("showing chars 0–1000 of 60000 — call again with "
                    "offset=1000 to continue reading")
    chunk, truncated, hint = sf._page(TEXT, 1000, 59500)
    assert chunk == TEXT[59500:]
    assert truncated is False
    assert hint is None
    chunk, truncated, hint = sf._page(TEXT, 1000, 70000)   # beyond the end
    assert chunk == ""
    assert truncated is False
    assert hint is None


class _FetchCtx:
    def __init__(self):
        self.config = {"tools": {"web": {}}}


def _serve_text(monkeypatch, text):
    """Deterministic fetch: SSRF clean, trafilatura path returns `text`."""
    async def ok(host):
        return None
    async def direct(self, url, timeout):
        return "<html><body>unused</body></html>"
    monkeypatch.setattr(sf, "ssrf_refusal", ok)
    monkeypatch.setattr(sf.WebFetch, "_fetch_direct", direct)
    monkeypatch.setattr(sf, "trafilatura", object())       # truthy: path enabled
    monkeypatch.setattr(sf, "extract_main_text", lambda h: text)


def _fetch(args):
    return asyncio.run(sf.WebFetch().execute(
        {"url": "https://ex.com/page", **args}, _FetchCtx()))


def test_fetch_truncation_hint_paginates(monkeypatch):
    _serve_text(monkeypatch, TEXT)
    res = _fetch({"max_chars": 1000})
    assert res.status == "ok"
    assert res.result["truncated"] is True
    assert res.result["content"] == TEXT[:1000]
    assert res.result["original_length"] == 60000
    assert "offset=1000" in res.result["hint"]
    assert "offset" not in res.result            # first page: no offset echo


def test_fetch_offset_reads_on(monkeypatch):
    _serve_text(monkeypatch, TEXT)
    res = _fetch({"max_chars": 1000, "offset": 59500})
    assert res.result["content"] == TEXT[59500:]
    assert res.result["truncated"] is False
    assert "hint" not in res.result              # done: no continuation hint
    assert res.result["offset"] == 59500


def test_fetch_offset_beyond_end(monkeypatch):
    _serve_text(monkeypatch, TEXT)
    res = _fetch({"max_chars": 1000, "offset": 70000})
    assert res.result["content"] == ""
    assert res.result["truncated"] is False


class _Resp:
    def __init__(self, body, ctype="text/plain"):
        self.status_code = 200
        self.headers = {"content-type": ctype}
        self._body = body.encode()

    async def aiter_bytes(self):
        yield self._body


class _StreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


def _fake_http(monkeypatch, resp):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kw):
            return _StreamCM(resp)
    monkeypatch.setattr(Q.httpx, "AsyncClient", _Client)
    async def ok(host):
        return None
    monkeypatch.setattr(Q, "ssrf_refusal", ok)


def _request(args):
    ctx = ToolContext(request_id="t", config={}, budget=None)
    return asyncio.run(Q.WebRequest().execute(
        {"url": "https://api.ex.com/data", **args}, ctx))


def test_request_text_body_paginates(monkeypatch):
    _fake_http(monkeypatch, _Resp(TEXT))
    res = _request({"max_chars": 1000})
    assert res.status == "ok"
    assert res.result["truncated"] is True
    assert res.result["body"] == TEXT[:1000]
    assert res.result["hint"].endswith("offset=1000 to continue reading")
    res = _request({"max_chars": 1000, "offset": 59000})
    assert res.result["body"] == TEXT[59000:]
    assert res.result["truncated"] is False
    assert "hint" not in res.result


def test_request_json_body_no_pagination_hint(monkeypatch):
    """JSON parses whole (the envelope caps serialization) — no offset hint."""
    _fake_http(monkeypatch, _Resp('{"big": "%s"}' % ("x" * 60000),
                                  "application/json"))
    res = _request({"max_chars": 1000})
    assert res.status == "ok"
    assert "json" in res.result
    assert "hint" not in res.result


def test_render_offset_paginates(monkeypatch):
    async def render_html(bcfg, url, **kw):
        return "<html><body>unused</body></html>", "t"
    monkeypatch.setattr(R.session, "render_html", render_html)
    monkeypatch.setattr(R, "html_to_text", lambda h: TEXT)
    async def ok(host):
        return None
    monkeypatch.setattr(R, "ssrf_refusal", ok)
    ctx = ToolContext(request_id="t", config={"tools": {"web": {}}}, budget=None)
    res = asyncio.run(R.WebRender().execute(
        {"url": "https://ex.com/app", "max_chars": 1000}, ctx))
    assert res.status == "ok"
    assert res.result["truncated"] is True
    assert res.result["content"] == TEXT[:1000]
    assert "offset=1000" in res.result["hint"]
    res = asyncio.run(R.WebRender().execute(
        {"url": "https://ex.com/app", "max_chars": 1000, "offset": 59500}, ctx))
    assert res.result["content"] == TEXT[59500:]
    assert res.result["truncated"] is False
    assert "hint" not in res.result
