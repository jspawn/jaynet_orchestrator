"""Shared headless-browser session for web.render and the browser.* tools.

Playwright's *bundled* Chromium is built for Ubuntu and does not run on Arch /
CachyOS (library + symbol mismatches), so we never use it. Instead this module
resolves a browser one of two ways:

  1. A containerized Playwright over CDP — set tools.browser.ws_endpoint or the
     BROWSER_WS env var. PREFERRED for the long-lived service: the host stays
     clean and the Chromium/Playwright versions are matched inside the image.
       docker run -d --rm --name pw -p 9222:9222 \
         mcr.microsoft.com/playwright:v1.52.0-jammy \
         npx -y playwright run-server --port 9222 --host 0.0.0.0
       export BROWSER_WS=ws://127.0.0.1:9222/
  2. Otherwise a LOCAL launch using the SYSTEM Chromium binary
     (tools.browser.executable_path, or an auto-detected /usr/bin/chromium):
       sudo pacman -S chromium

One browser is acquired lazily and reused across calls; LOCK serializes page
work so only one render runs at a time on a box that's also doing inference.
"""
from __future__ import annotations

import asyncio
import os
import shutil

LOCK = asyncio.Lock()
_play = None
_browser = None

_UA = "Mozilla/5.0 (X11; Linux x86_64) JayNetOrchestrator/1.0 (headless)"
_CHROMIUM_CANDIDATES = ("chromium", "chromium-browser",
                        "google-chrome-stable", "google-chrome")


def browser_cfg(config: dict) -> dict:
    """The tools.browser config block (how to *get* a browser)."""
    return (config.get("tools", {}).get("browser", {}) or {})


def system_chromium(cfg: dict) -> str | None:
    """Resolve a system Chromium path: explicit config/env first, then PATH."""
    cand = cfg.get("executable_path") or os.environ.get("CHROMIUM_PATH")
    if cand:
        return cand
    for name in _CHROMIUM_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def ws_endpoint(cfg: dict) -> str | None:
    return (cfg.get("ws_endpoint") or os.environ.get("BROWSER_WS")
            or os.environ.get("PLAYWRIGHT_WS"))


def launch_kwargs(cfg: dict) -> dict:
    """Args for chromium.launch — includes executable_path iff a system binary
    is found, so on a box without one we fall back to Playwright's bundled build
    (fine on Ubuntu CI)."""
    kw: dict = {"headless": bool(cfg.get("headless", True))}
    exe = system_chromium(cfg)
    if exe:
        kw["executable_path"] = exe
    if cfg.get("args"):
        kw["args"] = list(cfg["args"])
    return kw


async def get_browser(cfg: dict):
    """Return a connected Playwright Browser, connecting/launching on first use."""
    global _play, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    _browser = None
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(
            "playwright is not installed. Install it into the orchestrator venv: "
            f"`uv pip install --python {__import__('runtime.paths', fromlist=['VENV_PYTHON']).VENV_PYTHON} playwright`."
        ) from e
    if _play is None:
        _play = await async_playwright().start()

    ws = ws_endpoint(cfg)
    if ws:
        try:
            _browser = await _play.chromium.connect_over_cdp(ws)
        except Exception as e:
            raise RuntimeError(
                f"could not connect to the browser container at {ws} "
                f"({type(e).__name__}: {e}). Is the Playwright container running?"
            ) from e
        return _browser

    kw = launch_kwargs(cfg)
    try:
        _browser = await _play.chromium.launch(**kw)
    except Exception as e:
        if "executable_path" in kw:
            hint = (f"system Chromium at {kw['executable_path']} failed to launch — "
                    "check it runs headless: `chromium --headless=new --dump-dom https://example.com`.")
        else:
            hint = ("on Arch/CachyOS the bundled Chromium does not run. Install system "
                    "Chromium (`sudo pacman -S chromium`) and set "
                    "tools.browser.executable_path: /usr/bin/chromium — or run a Playwright "
                    "container and set BROWSER_WS / tools.browser.ws_endpoint.")
        raise RuntimeError(f"could not start a browser ({type(e).__name__}: {e}); {hint}") from e
    return _browser


async def close() -> None:
    """Tear down the shared browser (best-effort; e.g. on shutdown)."""
    global _browser
    try:
        if _browser is not None and _browser.is_connected():
            await _browser.close()
    except Exception:
        pass
    finally:
        _browser = None


async def _settle(page, *, wait_selector, nav_timeout_ms, wait_ms):
    if wait_selector:
        await page.wait_for_selector(wait_selector, timeout=nav_timeout_ms)
    if wait_ms:
        await page.wait_for_timeout(wait_ms)


async def render_html(cfg, url, *, wait_until, nav_timeout_ms,
                      wait_selector=None, wait_ms=0):
    """Open the URL in a fresh context, let JS run, return (html, title)."""
    browser = await get_browser(cfg)
    context = await browser.new_context(user_agent=_UA)
    page = await context.new_page()
    try:
        await page.goto(url, wait_until=wait_until, timeout=nav_timeout_ms)
        await _settle(page, wait_selector=wait_selector,
                      nav_timeout_ms=nav_timeout_ms, wait_ms=wait_ms)
        return await page.content(), await page.title()
    finally:
        await context.close()


async def capture(cfg, url, *, kind, wait_until, nav_timeout_ms,
                  wait_selector=None, wait_ms=0, full_page=True,
                  viewport=None, pdf_format="A4"):
    """Return (bytes, title): a PNG screenshot (kind='screenshot') or a PDF
    (kind='pdf', headless only)."""
    browser = await get_browser(cfg)
    ctx_kw = {"user_agent": _UA}
    if viewport:
        ctx_kw["viewport"] = viewport
    context = await browser.new_context(**ctx_kw)
    page = await context.new_page()
    try:
        await page.goto(url, wait_until=wait_until, timeout=nav_timeout_ms)
        await _settle(page, wait_selector=wait_selector,
                      nav_timeout_ms=nav_timeout_ms, wait_ms=wait_ms)
        title = await page.title()
        if kind == "pdf":
            data = await page.pdf(format=pdf_format, print_background=True)
        else:
            data = await page.screenshot(full_page=full_page, type="png")
        return data, title
    finally:
        await context.close()
