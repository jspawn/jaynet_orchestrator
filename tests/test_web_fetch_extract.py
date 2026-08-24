"""web.fetch extraction: trafilatura main-content extraction is the default
(drops nav/footer/cookie boilerplate a plain tag-strip would send to the
model), with the regex tag-strip as fallback when trafilatura finds nothing,
is disabled, or isn't installed."""
import asyncio

import pytest

from tools.web import search_fetch as sf


class _Ctx:
    def __init__(self, cfg=None):
        self.config = {"tools": {"web": cfg or {}}}


_BOILER_HTML = (
    "<html><body>"
    "<nav>Home | Products | About | Contact</nav>"
    "<div class='cookie-banner'>We use cookies! Accept all.</div>"
    "<article><h1>Local Models</h1>"
    "<p>JayNet runs local models first. The orchestrator routes tasks "
    "to a brain and a specialist over a unified proxy.</p></article>"
    "<footer>Copyright 2026 — impressum, privacy policy, careers</footer>"
    "</body></html>")


def _fetch(html, cfg=None):
    return asyncio.run(sf.WebFetch().execute({"url": "https://ex.com/page"},
                                             _Ctx(cfg)))


@pytest.fixture
def _no_ssrf(monkeypatch):
    """Keep the test off DNS/network: the SSRF pre-check is stubbed clean and
    _fetch_direct serves the fixture HTML."""
    async def ok(host):
        return None
    monkeypatch.setattr(sf, "ssrf_refusal", ok)


def _serve(monkeypatch, html):
    async def direct(self, url, timeout):
        return html
    monkeypatch.setattr(sf.WebFetch, "_fetch_direct", direct)


def test_extract_main_text_drops_boilerplate():
    text = sf.extract_main_text(_BOILER_HTML)
    assert text is not None
    assert "local models first" in text.lower()
    for junk in ("cookie", "impressum", "Products"):
        assert junk not in text


def test_execute_prefers_trafilatura(monkeypatch, _no_ssrf):
    _serve(monkeypatch, _BOILER_HTML)
    res = _fetch(_BOILER_HTML)
    assert res.status == "ok"
    assert res.result["via"] == "trafilatura"
    assert "local models first" in res.result["content"].lower()
    assert "cookie" not in res.result["content"]


def test_execute_falls_back_to_tagstrip(monkeypatch, _no_ssrf):
    """No main content found → the plain strip still returns the page."""
    _serve(monkeypatch, _BOILER_HTML)
    monkeypatch.setattr(sf, "extract_main_text", lambda h: None)
    res = _fetch(_BOILER_HTML)
    assert res.status == "ok"
    assert res.result["via"] == "direct"
    assert "local models first" in res.result["content"].lower()
    assert "cookie" in res.result["content"]      # strip keeps boilerplate


def test_execute_trafilatura_disabled_by_config(monkeypatch, _no_ssrf):
    _serve(monkeypatch, _BOILER_HTML)
    res = _fetch(_BOILER_HTML, {"trafilatura_enabled": False})
    assert res.result["via"] == "direct"


def test_execute_trafilatura_not_installed(monkeypatch, _no_ssrf):
    """Partial/minimal install without the dep: silently the old behavior."""
    _serve(monkeypatch, _BOILER_HTML)
    monkeypatch.setattr(sf, "trafilatura", None)
    res = _fetch(_BOILER_HTML)
    assert res.status == "ok"
    assert res.result["via"] == "direct"
