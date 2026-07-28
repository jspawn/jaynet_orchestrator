"""Tests for the shared browser session resolution + the browser.* tools.
The real headless browser is never launched here — session.capture is mocked."""
import pytest
from conftest import run
from runtime.tool_base import ToolContext
from runtime.outputs import read_manifest
from tools.browser import session
from tools.browser.tools import BrowserScreenshot, BrowserPdf
from tools.web.render import WebRender


# ---- pure browser-resolution logic ----
def test_system_chromium_prefers_explicit(monkeypatch):
    monkeypatch.delenv("CHROMIUM_PATH", raising=False)
    assert session.system_chromium({"executable_path": "/usr/bin/chromium"}) == "/usr/bin/chromium"

def test_system_chromium_env(monkeypatch):
    monkeypatch.setenv("CHROMIUM_PATH", "/opt/chromium")
    assert session.system_chromium({}) == "/opt/chromium"

def test_system_chromium_path_scan(monkeypatch):
    monkeypatch.delenv("CHROMIUM_PATH", raising=False)
    monkeypatch.setattr(session.shutil, "which",
                        lambda n: "/usr/bin/chromium" if n == "chromium" else None)
    assert session.system_chromium({}) == "/usr/bin/chromium"

def test_ws_endpoint_env(monkeypatch):
    monkeypatch.setenv("BROWSER_WS", "ws://127.0.0.1:9222/")
    assert session.ws_endpoint({}) == "ws://127.0.0.1:9222/"
    assert session.ws_endpoint({"ws_endpoint": "ws://cfg/"}) == "ws://cfg/"   # cfg wins

def test_launch_kwargs_includes_executable(monkeypatch):
    monkeypatch.delenv("CHROMIUM_PATH", raising=False)
    kw = session.launch_kwargs({"executable_path": "/usr/bin/chromium", "headless": True})
    assert kw["executable_path"] == "/usr/bin/chromium" and kw["headless"] is True

def test_launch_kwargs_no_binary_falls_back(monkeypatch):
    monkeypatch.delenv("CHROMIUM_PATH", raising=False)
    monkeypatch.setattr(session.shutil, "which", lambda n: None)
    kw = session.launch_kwargs({})
    assert "executable_path" not in kw   # -> Playwright's bundled build (CI)


# ---- tool integration (capture mocked) ----
def _ctx(tmp_path, **browser):
    emitted = []
    async def emit(t, d): emitted.append((t, d))
    cfg = {"web": {"outputs_dir": str(tmp_path / "outputs"), "max_output_mb": 50},
           "tools": {"browser": {"enabled": True, **browser}}}
    ctx = ToolContext(request_id="run123", config=cfg, budget=None, owner="alice", emit=emit)
    return ctx, emitted


def test_screenshot_delivers_and_emits(tmp_path, monkeypatch):
    async def fake_capture(cfg, url, **kw):
        assert kw["kind"] == "screenshot"
        return (b"\x89PNG\r\n\x1a\nFAKEPNGDATA", "Example Domain")
    monkeypatch.setattr(session, "capture", fake_capture)
    ctx, emitted = _ctx(tmp_path)
    r = run(BrowserScreenshot().execute({"url": "https://example.com"}, ctx))
    assert r.status == "ok"
    assert r.result["name"].endswith(".png") and r.result["title"] == "Example Domain"
    assert r.result["download_url"] == "/api/output/run123"
    # staged + download chip emitted
    m = read_manifest(str(tmp_path / "outputs"), "run123")
    assert m and m["owner"] == "alice"
    assert emitted and emitted[0][0] == "output"


def test_pdf_delivers(tmp_path, monkeypatch):
    async def fake_capture(cfg, url, **kw):
        assert kw["kind"] == "pdf"
        return (b"%PDF-1.4 fake", "Report")
    monkeypatch.setattr(session, "capture", fake_capture)
    ctx, _ = _ctx(tmp_path)
    r = run(BrowserPdf().execute({"url": "https://example.com/r", "format": "Letter"}, ctx))
    assert r.status == "ok" and r.result["name"].endswith(".pdf")


def test_browser_disabled(tmp_path):
    ctx, _ = _ctx(tmp_path, enabled=False)
    r = run(BrowserScreenshot().execute({"url": "https://example.com"}, ctx))
    assert r.status == "error" and "disabled" in r.error

def test_browser_bad_scheme(tmp_path):
    ctx, _ = _ctx(tmp_path)
    r = run(BrowserScreenshot().execute({"url": "ftp://nope"}, ctx))
    assert r.status == "error" and "scheme" in r.error


def test_screenshot_refuses_metadata_target(tmp_path):
    ctx, _ = _ctx(tmp_path)
    r = run(BrowserScreenshot().execute(
        {"url": "http://169.254.169.254/latest/meta-data/"}, ctx))
    assert r.status == "error" and "link-local" in r.error


def test_screenshot_refuses_hostname_to_loopback(tmp_path, monkeypatch):
    async def fake_resolve(host):
        return ["127.0.0.1"]
    import tools.web.search_fetch as sf
    monkeypatch.setattr(sf, "_resolve_ips", fake_resolve)
    ctx, _ = _ctx(tmp_path)
    r = run(BrowserScreenshot().execute({"url": "http://evil.example:8080/"}, ctx))
    assert r.status == "error" and "loopback" in r.error


def test_web_render_refuses_loopback(tmp_path):
    ctx = ToolContext(request_id="r", config={}, budget=None)
    r = run(WebRender().execute({"url": "http://127.0.0.1:8071/"}, ctx))
    assert r.status == "error" and "loopback" in r.error

def test_screenshot_surfaces_browser_error(tmp_path, monkeypatch):
    async def boom(cfg, url, **kw):
        raise RuntimeError("could not start a browser; install system Chromium")
    monkeypatch.setattr(session, "capture", boom)
    ctx, _ = _ctx(tmp_path)
    r = run(BrowserScreenshot().execute({"url": "https://example.com"}, ctx))
    assert r.status == "error" and "Chromium" in r.error


# ---- web.render now rides the shared session ----
def test_web_render_uses_session(tmp_path, monkeypatch):
    async def fake_render(cfg, url, **kw):
        return ("<html><body>Rendered content here</body></html>", "Title")
    monkeypatch.setattr(session, "render_html", fake_render)
    cfg = {"tools": {"web": {"render": {"enabled": True}, "max_content_chars": 50000},
                     "browser": {"executable_path": "/usr/bin/chromium"}}}
    ctx = ToolContext(request_id="r", config=cfg, budget=None)
    r = run(WebRender().execute({"url": "https://spa.example"}, ctx))
    assert r.status == "ok" and "Rendered content" in r.result["content"]
    assert r.result["via"] == "render"


# ---- return_image: show the capture to the model ----
def _fake_png(w=800, h=600):
    import struct
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = (struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h)
            + b"\x08\x06\x00\x00\x00" + b"\x00\x00\x00\x00")
    return sig + ihdr + b"FAKEPNGDATA"


def test_png_size():
    from tools.browser.tools import _png_size
    assert _png_size(_fake_png(800, 600)) == (800, 600)
    assert _png_size(b"not a png") == (0, 0)


def test_return_image_attaches_when_vision(tmp_path, monkeypatch):
    async def fake_capture(cfg, url, **kw):
        return (_fake_png(), "T")
    monkeypatch.setattr(session, "capture", fake_capture)
    ctx, _ = _ctx(tmp_path)
    ctx.vision_enabled = True
    r = run(BrowserScreenshot().execute({"url": "https://x.com", "return_image": True}, ctx))
    assert r.status == "ok" and r.result["shown_to_model"] is True
    assert len(r.images) == 1 and r.images[0].startswith("data:image/png;base64,")


def test_return_image_dropped_without_vision(tmp_path, monkeypatch):
    async def fake_capture(cfg, url, **kw):
        return (_fake_png(), "T")
    monkeypatch.setattr(session, "capture", fake_capture)
    ctx, _ = _ctx(tmp_path)          # vision_enabled defaults False
    r = run(BrowserScreenshot().execute({"url": "https://x.com", "return_image": True}, ctx))
    assert r.status == "ok" and r.images == [] and r.result["shown_to_model"] is False
    assert "no vision" in r.result["note"]


def test_return_image_over_pixel_budget(tmp_path, monkeypatch):
    async def fake_capture(cfg, url, **kw):
        return (_fake_png(4000, 3000), "T")
    monkeypatch.setattr(session, "capture", fake_capture)
    ctx, _ = _ctx(tmp_path)
    ctx.vision_enabled = True
    r = run(BrowserScreenshot().execute({"url": "https://x.com", "return_image": True}, ctx))
    assert r.images == [] and "exceeds the model-image budget" in r.result["note"]
