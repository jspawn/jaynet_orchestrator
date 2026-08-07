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


# ---- per-request SSRF guard (audit H2: in-browser redirect bypass) ----
class _FakeRoute:
    def __init__(self):
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


class _FakeRequest:
    def __init__(self, url):
        self.url = url


class _FakePwContext:
    """Just enough of a Playwright BrowserContext to capture the route handler."""
    def __init__(self):
        self.routes = {}

    async def route(self, pattern, handler):
        self.routes[pattern] = handler


def _installed_handler():
    ctx = _FakePwContext()
    run(session.install_ssrf_guard(ctx))
    return ctx.routes["**/*"]


def test_ssrf_guard_aborts_loopback_redirect():
    handler = _installed_handler()
    route = _FakeRoute()
    run(handler(route, _FakeRequest("http://127.0.0.1:8071/")))
    assert route.aborted and not route.continued


def test_ssrf_guard_aborts_metadata_target():
    handler = _installed_handler()
    route = _FakeRoute()
    run(handler(route, _FakeRequest("http://169.254.169.254/latest/meta-data/")))
    assert route.aborted and not route.continued


def test_ssrf_guard_allows_public_host_and_caches(monkeypatch):
    import tools.web.search_fetch as sf
    calls = []

    async def fake_resolve(host):
        calls.append(host)
        return ["93.184.216.34"]
    monkeypatch.setattr(sf, "_resolve_ips", fake_resolve)
    handler = _installed_handler()
    for _ in range(2):                      # second hit served from the cache
        route = _FakeRoute()
        run(handler(route, _FakeRequest("https://example.com/page")))
        assert route.continued and not route.aborted
    assert calls == ["example.com"]


def test_ssrf_guard_passes_non_network_urls():
    handler = _installed_handler()
    for url in ("about:blank", "data:text/html,<h1>hi</h1>", "blob:https://x.com/1"):
        route = _FakeRoute()
        run(handler(route, _FakeRequest(url)))
        assert route.continued and not route.aborted


def test_render_html_installs_guard(monkeypatch):
    """Wiring: the Playwright context render/capture work on must have the
    per-request guard installed before any navigation happens."""
    contexts = []

    class _Page:
        async def goto(self, *a, **kw): pass
        async def content(self): return "<html></html>"
        async def title(self): return "t"

    class _Ctx(_FakePwContext):
        async def new_page(self): return _Page()
        async def close(self): pass

    class _Browser:
        async def new_context(self, **kw):
            c = _Ctx()
            contexts.append(c)
            return c

    async def fake_get_browser(cfg):
        return _Browser()
    monkeypatch.setattr(session, "get_browser", fake_get_browser)
    html, title = run(session.render_html({}, "https://example.com",
                                          wait_until="load", nav_timeout_ms=1000))
    assert contexts and "**/*" in contexts[0].routes
    # ...and the installed handler really aborts a loopback redirect target
    route = _FakeRoute()
    run(contexts[0].routes["**/*"](route, _FakeRequest("http://127.0.0.1:8071/")))
    assert route.aborted


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
