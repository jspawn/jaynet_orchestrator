"""Video-platform tarpit guard: extraction/render on YouTube & co grinds for
minutes (networkidle never settles; rendered text is player chrome) — delta
timing 2026-09-27 measured 3.9m web.extract and long browser waits on
youtube.com. web.extract refuses early with an actionable message; render
downgrades to domcontentloaded and notes the limit."""

import asyncio

from runtime.tool_base import ToolContext
from tools.web.extract import WebExtract
from tools.web.render import WebRender
from tools.web.search_fetch import video_host
import tools.web.render as render_mod


def test_video_host_matching():
    assert video_host("youtube.com") == "youtube.com"
    assert video_host("www.youtube.com") == "youtube.com"
    assert video_host("m.youtube.com") == "youtube.com"
    assert video_host("youtu.be") == "youtu.be"
    assert video_host("music.youtube.com") == "youtube.com"
    assert video_host("wikipedia.org") is None
    assert video_host("") is None
    assert video_host("notyoutube.com") is None


def test_extract_refuses_video_hosts_fast():
    """No sub-agent spawn on video URLs — immediate actionable error."""
    def _boom_spawn(*a, **k):
        raise AssertionError("spawn must not be called for video hosts")
    ctx = ToolContext(request_id="t", config={}, budget=None)
    ctx.spawn = _boom_spawn
    r = asyncio.run(WebExtract().execute(
        {"url": "https://www.youtube.com/watch?v=L1vXCYZAYYM",
         "describe": "the transcript"}, ctx))
    assert r.status == "error"
    assert "video platform" in r.error
    assert "web.fetch" in r.error


def test_render_downgrades_wait_on_video_hosts(monkeypatch):
    """Default readiness on a video host becomes domcontentloaded."""
    monkeypatch.setattr(render_mod, "ssrf_refusal",
                        lambda *a, **k: _async_none())
    seen = {}

    async def _fake_render(bcfg, url, wait_until=None, nav_timeout_ms=None,
                           wait_selector=None, wait_ms=None):
        seen["wait_until"] = wait_until
        return "<html><body>player</body></html>", "t"
    monkeypatch.setattr(render_mod.session, "render_html", _fake_render)
    ctx = ToolContext(request_id="t", config={"tools": {"web": {}}},
                      budget=None)
    r = asyncio.run(WebRender().execute(
        {"url": "https://youtu.be/L1vXCYZAYYM"}, ctx))
    assert r.status == "ok"
    assert seen["wait_until"] == "domcontentloaded"
    assert "video platform" in r.result["note"]


def test_render_respects_explicit_wait_until(monkeypatch):
    """A caller who explicitly asks for networkidle keeps it."""
    monkeypatch.setattr(render_mod, "ssrf_refusal",
                        lambda *a, **k: _async_none())
    seen = {}

    async def _fake_render(bcfg, url, wait_until=None, nav_timeout_ms=None,
                           wait_selector=None, wait_ms=None):
        seen["wait_until"] = wait_until
        return "<html><body>player</body></html>", "t"
    monkeypatch.setattr(render_mod.session, "render_html", _fake_render)
    ctx = ToolContext(request_id="t", config={"tools": {"web": {}}},
                      budget=None)
    r = asyncio.run(WebRender().execute(
        {"url": "https://youtu.be/L1vXCYZAYYM", "wait_until": "load"}, ctx))
    assert r.status == "ok"
    assert seen["wait_until"] == "load"


async def _async_none():
    return None
