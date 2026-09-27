"""web.search empty-result fall-through: a backend returning [] is degraded,
not "no sources" — the chain continues, and if everything comes back empty
the tool errors loudly instead of letting the model hallucinate (live
incident 2026-09-27: Tavily over quota (HTTP 432) + DDG bot-blocked → six
searches returned ok+[] → brain invented answers about military tattoos)."""

import asyncio

from tools.web.search_fetch import WebSearch
from runtime.tool_base import ToolContext


def _ctx(cfg=None):
    return ToolContext(request_id="t", config=cfg or {}, budget=None)


def _run(tool, cfg=None):
    return asyncio.run(tool.execute({"query": "q"}, _ctx(cfg)))


def test_empty_falls_through_to_next_backend(monkeypatch):
    """searXNG empty + Tavily hits → ok with Tavily's results."""
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    async def _empty_searxng(self, endpoint, query, n):
        return []
    async def _tavily(self, query, n, cfg):
        return [{"title": "t", "url": "u", "snippet": "s"}]
    monkeypatch.setattr(WebSearch, "_search_searxng", _empty_searxng)
    monkeypatch.setattr(WebSearch, "_search_tavily", _tavily)
    cfg = {"tools": {"web": {"search_endpoint": "http://x/search"}}}
    r = _run(WebSearch(), cfg)
    assert r.status == "ok"
    assert r.result == [{"title": "t", "url": "u", "snippet": "s"}]


def test_all_empty_is_loud_error(monkeypatch):
    """Every backend empty/failing → error naming each backend, never ok+[]."""
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    async def _empty(self, *a):
        return []
    async def _boom(self, *a, **k):
        raise RuntimeError("HTTP 432 plan limit")
    monkeypatch.setattr(WebSearch, "_search_searxng", _empty)
    monkeypatch.setattr(WebSearch, "_search_tavily", _boom)
    monkeypatch.setattr(WebSearch, "_search_ddg", _empty)
    monkeypatch.setattr(WebSearch, "_search_browser", _empty)
    cfg = {"tools": {"web": {"search_endpoint": "http://x/search"}}}
    r = _run(WebSearch(), cfg)
    assert r.status == "error"
    assert "do NOT conclude" in r.error
    assert "searxng: 0 results" in r.error
    assert "tavily: RuntimeError" in r.error
    assert "ddg: 0 results" in r.error
    assert "browser: 0 results" in r.error


def test_ddg_last_resort_empty_also_errors(monkeypatch):
    """No searXNG, no Tavily key, DDG bot-block empty → error, not ok+[]."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    async def _empty(self, *a):
        return []
    monkeypatch.setattr(WebSearch, "_search_ddg", _empty)
    monkeypatch.setattr(WebSearch, "_search_browser", _empty)
    r = _run(WebSearch())
    assert r.status == "error"
    assert "ddg: 0 results" in r.error


def test_non_empty_first_backend_still_wins(monkeypatch):
    """Healthy searXNG short-circuits as before."""
    async def _searxng(self, endpoint, query, n):
        return [{"title": "a", "url": "b", "snippet": "c"}]
    monkeypatch.setattr(WebSearch, "_search_searxng", _searxng)
    cfg = {"tools": {"web": {"search_endpoint": "http://x/search"}}}
    r = _run(WebSearch(), cfg)
    assert r.status == "ok"
    assert r.result[0]["title"] == "a"


def test_browser_last_resort_returns_results(monkeypatch):
    """All lightweight backends empty → browser SERP scrape saves the call."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    async def _empty(self, *a):
        return []
    async def _browser(self, query, n, ctx):
        return [{"title": "b", "url": "u", "snippet": "s"}]
    monkeypatch.setattr(WebSearch, "_search_ddg", _empty)
    monkeypatch.setattr(WebSearch, "_search_browser", _browser)
    r = _run(WebSearch())
    assert r.status == "ok"
    assert r.result[0]["title"] == "b"


def test_browser_empty_joins_loud_error(monkeypatch):
    """Browser rung empty too → its line appears in the degraded error."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    async def _empty(self, *a):
        return []
    monkeypatch.setattr(WebSearch, "_search_ddg", _empty)
    monkeypatch.setattr(WebSearch, "_search_browser", _empty)
    r = _run(WebSearch())
    assert r.status == "error"
    assert "browser: 0 results" in r.error


def test_browser_search_disabled_skips_rung(monkeypatch):
    """browser_search: false → the browser is never tried."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    called = []

    async def _empty(self, *a):
        return []

    async def _browser(self, query, n, ctx):
        called.append(1)
        return [{"title": "b", "url": "u", "snippet": "s"}]
    monkeypatch.setattr(WebSearch, "_search_ddg", _empty)
    monkeypatch.setattr(WebSearch, "_search_browser", _browser)
    r = _run(WebSearch(), {"tools": {"web": {"browser_search": False}}})
    assert r.status == "error"
    assert called == []
    assert "browser" not in r.error
