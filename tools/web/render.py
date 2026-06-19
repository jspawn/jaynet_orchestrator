"""web.render — fetch a JS-rendered page with a headless browser.

web.fetch only sees the static HTML the server sends; single-page apps,
dashboards, and many government geoportals return almost nothing useful that way
(often just a <title>). web.render drives a headless Chromium via Playwright,
waits for the page to settle, and returns the text *after* JavaScript has run.

It is heavier and slower than web.fetch — reach for it only when web.fetch comes
back thin (just a title / empty body) on a page you have reason to believe is
real and content-bearing.

Requires Playwright + a browser binary on the host:
    uv pip install --python /srv/orchestrator/litellmenv/bin/python playwright
    /srv/orchestrator/litellmenv/bin/python -m playwright install chromium
If Playwright or the browser is missing, the tool returns an actionable error
instead of crashing the run.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from runtime.tool_base import Tool, ToolContext, ToolResult
from .search_fetch import html_to_text

_UA = "Mozilla/5.0 (X11; Linux x86_64) JayNetOrchestrator/1.0 (headless)"

# A single Chromium is launched lazily and reused across calls; renders are
# serialized through _lock so one page runs at a time (simple + resource-safe on
# a box that's also doing inference).
_browser = None
_play = None
_lock = asyncio.Lock()


async def _ensure_browser(headless: bool):
    global _browser, _play
    if _browser is not None and _browser.is_connected():
        return _browser
    _browser = None
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(
            "playwright is not installed. Install it once: "
            "`uv pip install --python /srv/orchestrator/litellmenv/bin/python playwright` "
            "then `… -m playwright install chromium`."
        ) from e
    if _play is None:
        _play = await async_playwright().start()
    try:
        _browser = await _play.chromium.launch(headless=headless)
    except Exception as e:
        raise RuntimeError(
            f"could not launch headless Chromium ({type(e).__name__}: {e}). "
            "Run `… -m playwright install chromium` to install the browser binary."
        ) from e
    return _browser


async def _render_page(url: str, *, wait_until: str, nav_timeout_ms: int,
                       wait_selector: str | None, wait_ms: int, headless: bool):
    """Open the URL in a fresh context, let JS run, return (html, title)."""
    browser = await _ensure_browser(headless)
    context = await browser.new_context(user_agent=_UA)
    page = await context.new_page()
    try:
        await page.goto(url, wait_until=wait_until, timeout=nav_timeout_ms)
        if wait_selector:
            await page.wait_for_selector(wait_selector, timeout=nav_timeout_ms)
        if wait_ms:
            await page.wait_for_timeout(wait_ms)
        return await page.content(), await page.title()
    finally:
        await context.close()


class WebRender(Tool):
    name = "web.render"
    description = (
        "Fetch a URL through a headless browser and return the page text AFTER "
        "JavaScript runs. Use only when web.fetch returns thin or empty content on "
        "a JS-heavy page (single-page apps, dashboards, geoportals). Slower and "
        "heavier than web.fetch — always try web.fetch first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL (with https://)."},
            "max_chars": {"type": "integer", "default": 20000, "minimum": 500, "maximum": 100000},
            "wait_until": {
                "type": "string", "enum": ["load", "domcontentloaded", "networkidle"],
                "default": "networkidle",
                "description": "When the page is considered ready. networkidle waits for XHRs to settle.",
            },
            "wait_selector": {
                "type": "string",
                "description": "Optional CSS selector to wait for before reading (e.g. a results container).",
            },
            "wait_ms": {
                "type": "integer", "minimum": 0, "maximum": 15000,
                "description": "Optional extra wait after load, in ms, for late-rendering content.",
            },
        },
        "required": ["url"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        url = args["url"]
        if urlparse(url).scheme not in ("http", "https"):
            return ToolResult(status="error", result=None,
                              error=f"unsupported scheme in URL: {url!r}")

        web_cfg = ctx.config.get("tools", {}).get("web", {})
        cfg = web_cfg.get("render", {})
        if not cfg.get("enabled", True):
            return ToolResult(status="error", result=None,
                              error="web.render is disabled (tools.web.render.enabled=false).")

        cap = min(int(args.get("max_chars", 20000)), web_cfg.get("max_content_chars", 50000))
        nav_timeout_ms = int(cfg.get("nav_timeout_s", 30)) * 1000
        wait_until = args.get("wait_until") or cfg.get("wait_until", "networkidle")
        wait_ms = int(args.get("wait_ms") or 0)

        async with _lock:
            try:
                html, title = await _render_page(
                    url, wait_until=wait_until, nav_timeout_ms=nav_timeout_ms,
                    wait_selector=args.get("wait_selector"), wait_ms=wait_ms,
                    headless=cfg.get("headless", True))
            except RuntimeError as e:        # playwright / browser not available
                return ToolResult(status="error", result=None, error=str(e))
            except Exception as e:           # navigation/timeout/etc.
                return ToolResult(status="error", result=None,
                                  error=f"render failed: {type(e).__name__}: {e}")

        text = html_to_text(html)
        truncated = len(text) > cap
        return ToolResult(status="ok", result={
            "url": url, "title": title, "content": text[:cap],
            "truncated": truncated, "original_length": len(text), "via": "render",
        })
