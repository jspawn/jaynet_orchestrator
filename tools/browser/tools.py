"""browser.* — visual capture from a headless browser (screenshot, PDF).

Shares the Arch-friendly browser session with web.render (tools/browser/
session.py): connect to a containerized Playwright over CDP if BROWSER_WS /
tools.browser.ws_endpoint is set, else launch the system Chromium binary —
never Playwright's bundled Ubuntu build, which doesn't run on Arch.

web.render returns page TEXT after JS; these return an IMAGE / PDF of the page,
delivered to the user as a download (and an inline preview, since PNG and PDF
are previewable). Read-only network fetches like web.fetch / web.render, so not
confirmation-gated.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import urlparse

from runtime.outputs import OutputTooLarge, stage_and_bundle
from runtime.tool_base import Tool, ToolContext, ToolResult

from . import session


def _slug(url: str, ext: str) -> str:
    host = (urlparse(url).hostname or "page").replace(".", "-")
    return f"{host}.{ext}"


def _common_props() -> dict:
    return {
        "url": {"type": "string", "description": "Full URL (with https://)."},
        "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"],
                       "default": "networkidle",
                       "description": "When the page is considered ready."},
        "wait_selector": {"type": "string",
                          "description": "Optional CSS selector to wait for before capturing."},
        "wait_ms": {"type": "integer", "minimum": 0, "maximum": 15000,
                    "description": "Optional extra settle time after load, in ms."},
    }


def _guard(args: dict, ctx: ToolContext):
    url = args.get("url", "")
    if urlparse(url).scheme not in ("http", "https"):
        return None, ToolResult(status="error", result=None, tool_name="browser",
                                error=f"unsupported scheme in URL: {url!r}")
    bcfg = session.browser_cfg(ctx.config)
    if not bcfg.get("enabled", True):
        return None, ToolResult(status="error", result=None, tool_name="browser",
                                error="browser tools are disabled (tools.browser.enabled=false).")
    return bcfg, None


async def _deliver(ctx: ToolContext, data: bytes, name: str) -> dict:
    web = ctx.config.get("web", {}) or {}
    from runtime.paths import OUTPUTS_DIR
    outputs_dir = web.get("outputs_dir", str(OUTPUTS_DIR))
    max_mb = int(web.get("max_output_mb", 200))
    tmp = Path(tempfile.mkdtemp(prefix="browser-")) / name
    tmp.write_bytes(data)
    manifest = stage_and_bundle(outputs_dir, ctx.request_id, ctx.owner,
                                [str(tmp)], None, max_mb * 1024 * 1024)
    if ctx.emit is not None:
        await ctx.emit("output", {"run_id": ctx.request_id, "name": manifest["name"],
                                  "size": manifest["size"], "kind": manifest["kind"]})
    return manifest


class BrowserScreenshot(Tool):
    name = "browser.screenshot"
    description = (
        "Capture a PNG screenshot of a web page after JavaScript renders, and "
        "deliver it to the user as a downloadable / previewable image. Use for "
        "visual snapshots — layouts, charts, dashboards, proof of a page's state. "
        "For page TEXT, use web.render instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            **_common_props(),
            "full_page": {"type": "boolean", "default": True,
                          "description": "Capture the whole scrollable page, not just the viewport."},
            "width": {"type": "integer", "minimum": 320, "maximum": 3840, "default": 1280},
            "height": {"type": "integer", "minimum": 320, "maximum": 3840, "default": 800},
        },
        "required": ["url"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        bcfg, err = _guard(args, ctx)
        if err:
            return err
        nav_ms = int(bcfg.get("nav_timeout_s", 30)) * 1000
        viewport = {"width": int(args.get("width", 1280)), "height": int(args.get("height", 800))}
        async with session.LOCK:
            try:
                data, title = await session.capture(
                    bcfg, args["url"], kind="screenshot",
                    wait_until=args.get("wait_until", "networkidle"), nav_timeout_ms=nav_ms,
                    wait_selector=args.get("wait_selector"), wait_ms=int(args.get("wait_ms") or 0),
                    full_page=bool(args.get("full_page", True)), viewport=viewport)
            except RuntimeError as e:          # browser/playwright unavailable
                return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
            except Exception as e:             # navigation/timeout/etc.
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"screenshot failed: {type(e).__name__}: {e}")
        try:
            m = await _deliver(ctx, data, _slug(args["url"], "png"))
        except OutputTooLarge as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"screenshot too large ({e.size} bytes)")
        return ToolResult(status="ok", result={
            "url": args["url"], "title": title, "name": m["name"], "size": m["size"],
            "download_url": f"/api/output/{ctx.request_id}",
            "note": "PNG delivered to the user (preview/download); kept only if they save the chat."},
            tool_name=self.name)


class BrowserPdf(Tool):
    name = "browser.pdf"
    description = (
        "Render a web page to a PDF after JavaScript runs, and deliver it to the "
        "user as a downloadable / previewable file. Good for archiving an article "
        "or report as it appears in the browser. For page TEXT, use web.render."
    )
    parameters = {
        "type": "object",
        "properties": {
            **_common_props(),
            "format": {"type": "string", "enum": ["A4", "Letter", "Legal"], "default": "A4"},
        },
        "required": ["url"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        bcfg, err = _guard(args, ctx)
        if err:
            return err
        nav_ms = int(bcfg.get("nav_timeout_s", 30)) * 1000
        async with session.LOCK:
            try:
                data, title = await session.capture(
                    bcfg, args["url"], kind="pdf",
                    wait_until=args.get("wait_until", "networkidle"), nav_timeout_ms=nav_ms,
                    wait_selector=args.get("wait_selector"), wait_ms=int(args.get("wait_ms") or 0),
                    pdf_format=args.get("format", "A4"))
            except RuntimeError as e:
                return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
            except Exception as e:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"pdf failed: {type(e).__name__}: {e}")
        try:
            m = await _deliver(ctx, data, _slug(args["url"], "pdf"))
        except OutputTooLarge as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"pdf too large ({e.size} bytes)")
        return ToolResult(status="ok", result={
            "url": args["url"], "title": title, "name": m["name"], "size": m["size"],
            "download_url": f"/api/output/{ctx.request_id}",
            "note": "PDF delivered to the user (preview/download); kept only if they save the chat."},
            tool_name=self.name)
