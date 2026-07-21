"""web.search backend priority: SearxNG (local, if search_endpoint set) ->
Tavily (if TAVILY_API_KEY set) -> DuckDuckGo. Each backend falls through to the
next on failure. Backends are monkeypatched — no network."""
import asyncio

import tools.web.search_fetch as M
from tools.web.search_fetch import WebSearch


class _Ctx:
    config = {"tools": {"web": {"search_endpoint": "http://searx.local/search"}}}


def _run():
    return asyncio.run(WebSearch().execute({"query": "q"}, _Ctx()))


def _stub(monkeypatch, *, searxng=None, tavily=None, ddg=None):
    """Each value: list to return, or Exception instance to raise."""
    for name, result in (("searxng", searxng), ("tavily", tavily), ("ddg", ddg)):
        if result is None:
            continue
        async def fake(*a, _r=result, **k):
            if isinstance(_r, Exception):
                raise _r
            return _r
        monkeypatch.setattr(WebSearch, f"_search_{name}", fake)


def test_searxng_wins_over_tavily(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    _stub(monkeypatch, searxng=[{"via": "searxng"}], tavily=[{"via": "tavily"}])
    res = _run()
    assert res.status == "ok"
    assert res.result == [{"via": "searxng"}]


def test_searxng_failure_falls_through_to_tavily(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    _stub(monkeypatch, searxng=RuntimeError("down"), tavily=[{"via": "tavily"}])
    res = _run()
    assert res.status == "ok"
    assert res.result == [{"via": "tavily"}]


def test_all_backends_down_falls_to_ddg(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    _stub(monkeypatch, searxng=RuntimeError("down"), tavily=RuntimeError("down"),
          ddg=[{"via": "ddg"}])
    res = _run()
    assert res.status == "ok"
    assert res.result == [{"via": "ddg"}]


def test_all_backends_failing_reports_each(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    _stub(monkeypatch, searxng=RuntimeError("boom"), ddg=RuntimeError("bust"))
    res = _run()
    assert res.status == "error"
    assert "searxng" in res.error and "ddg" in res.error


def test_no_endpoint_skips_searxng(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    _stub(monkeypatch, searxng=[{"via": "searxng"}], ddg=[{"via": "ddg"}])
    ctx = _Ctx()
    ctx.config = {"tools": {"web": {}}}
    res = asyncio.run(WebSearch().execute({"query": "q"}, ctx))
    assert res.result == [{"via": "ddg"}]
