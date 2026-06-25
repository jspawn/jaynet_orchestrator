"""web.render — fetch a JS-rendered page with a headless browser.

web.fetch only sees the static HTML the server sends; single-page apps,
dashboards, and many government geoportals return almost nothing useful that way
(often just a <title>). web.render drives a headless Chromium via Playwright,
waits for the page to settle, and returns the text *after* JavaScript has run.

It is heavier and slower than web.fetch — reach for it only when web.fetch comes
back thin (just a title / empty body) on a page you have reason to believe is
real and content-bearing.

The browser itself is resolved by tools/browser/session.py (a containerized
Playwright over CDP, or the system Chromium binary) — never Playwright's bundled
Ubuntu build, which doesn't run on Arch. If no browser is available the tool
returns an actionable error instead of crashing the run.
"""
from __future__ import annotations

from urllib.parse import urlparse

from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.browser import session
from .search_fetch import html_to_text


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
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"unsupported scheme in URL: {url!r}")

        web_cfg = ctx.config.get("tools", {}).get("web", {})
        cfg = web_cfg.get("render", {})
        if not cfg.get("enabled", True):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="web.render is disabled (tools.web.render.enabled=false).")

        # How to *get* a browser lives in tools.browser (shared with browser.*);
        # render-specific knobs (timeouts, readiness) stay here.
        bcfg = session.browser_cfg(ctx.config)
        cap = min(int(args.get("max_chars", 20000)), web_cfg.get("max_content_chars", 50000))
        nav_timeout_ms = int(cfg.get("nav_timeout_s", 30)) * 1000
        wait_until = args.get("wait_until") or cfg.get("wait_until", "networkidle")
        wait_ms = int(args.get("wait_ms") or 0)

        async with session.LOCK:
            try:
                html, title = await session.render_html(
                    bcfg, url, wait_until=wait_until, nav_timeout_ms=nav_timeout_ms,
                    wait_selector=args.get("wait_selector"), wait_ms=wait_ms)
            except RuntimeError as e:        # playwright / browser not available
                return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
            except Exception as e:           # navigation/timeout/etc.
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"render failed: {type(e).__name__}: {e}")

        text = html_to_text(html)
        truncated = len(text) > cap
        return ToolResult(status="ok", result={
            "url": url, "title": title, "content": text[:cap],
            "truncated": truncated, "original_length": len(text), "via": "render",
        }, tool_name=self.name)
