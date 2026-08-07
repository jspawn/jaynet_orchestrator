"""pdf.create: dependency-free MD->HTML + render via the shared Chromium session."""
import asyncio
import pytest
import tools.pdf.create as M
from tools.pdf.create import PdfCreate, _block_network, _md_to_html
from runtime.tool_base import ToolContext


def test_md_to_html_tables_code_lists():
    html = _md_to_html("# Title\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n- one\n- two\n\n```py\nx=1\n```")
    assert "<h1>Title</h1>" in html
    assert "<table>" in html and "<th>A</th>" in html and "<td>2</td>" in html   # the thing xhtml2pdf choked on
    assert "<ul><li>one</li>" in html
    assert "<pre><code>x=1</code></pre>" in html


def test_md_to_html_escapes_and_inline():
    h = _md_to_html("**bold** and `code` and a <script> and [x](http://y)")
    assert "<strong>bold</strong>" in h and "<code>code</code>" in h
    assert "&lt;script&gt;" in h and '<a href="http://y">x</a>' in h


class _Page:
    async def set_content(self, html, **kw): self.html = html
    async def pdf(self, **kw): return b"%PDF-1.4 fake-bytes"
class _Ctxm:
    def __init__(self, page): self._p = page; self.routes = []
    async def new_page(self): return self._p
    async def route(self, pattern, handler): self.routes.append((pattern, handler))
    async def close(self): pass
class _Browser:
    def __init__(self, page): self._p = page; self.last_ctx = None
    async def new_context(self):
        self.last_ctx = _Ctxm(self._p)
        return self.last_ctx


def _wire(monkeypatch, page):
    async def fake_get_browser(cfg): return _Browser(page)
    monkeypatch.setattr(M.session, "get_browser", fake_get_browser)
    monkeypatch.setattr(M.session, "LOCK", asyncio.Lock())


def _run(args, root): return asyncio.run(PdfCreate().execute(args, ToolContext(
    request_id="t", config={"tools": {"browser": {}}}, budget=None, work_root=str(root))))


def test_creates_pdf_in_workspace(tmp_path, monkeypatch):
    (tmp_path / "FMO_Course.md").write_text("# FMO\n\n| Fee | % |\n|---|---|\n| Mgmt | 1.5 |\n")
    page = _Page(); _wire(monkeypatch, page)
    r = _run({"input": "FMO_Course.md"}, tmp_path)
    assert r.status == "ok" and r.result["path"] == "FMO_Course.pdf"
    pdf = tmp_path / "FMO_Course.pdf"
    assert pdf.is_file() and pdf.read_bytes().startswith(b"%PDF")
    assert "<table>" in page.html                    # tables reached Chrome's renderer


def test_custom_output_and_title(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("body")
    page = _Page(); _wire(monkeypatch, page)
    r = _run({"input": "a.md", "output": "Report", "title": "Investment Funds"}, tmp_path)
    assert r.result["path"] == "Report.pdf" and (tmp_path / "Report.pdf").is_file()
    assert "<h1>Investment Funds</h1>" in page.html


def test_html_input_passthrough(tmp_path, monkeypatch):
    (tmp_path / "p.html").write_text("<h2>Hi</h2>")
    page = _Page(); _wire(monkeypatch, page)
    _run({"input": "p.html"}, tmp_path)
    assert "<h2>Hi</h2>" in page.html and "<html><body>" in page.html


def test_missing_input_errors(tmp_path, monkeypatch):
    page = _Page(); _wire(monkeypatch, page)
    assert _run({"input": "nope.md"}, tmp_path).status == "error"


def test_path_escape_rejected(tmp_path, monkeypatch):
    page = _Page(); _wire(monkeypatch, page)
    assert _run({"input": "../../etc/passwd"}, tmp_path).status == "error"


def test_pdf_nearmiss_unique_resolves(tmp_path, monkeypatch):
    (tmp_path/"4FMT"/"2026").mkdir(parents=True)
    (tmp_path/"4FMT"/"2026"/"Sales_Pitch.md").write_text("# Pitch")
    page=_Page(); _wire(monkeypatch, page)
    # model guesses the WRONG directory (4FMO) but the right unique filename
    r=_run({"input":"4FMO/2026/Sales_Pitch.md","output":"4FMO/2026/Sales_Pitch.pdf"}, tmp_path)
    assert r.status=="ok"
    assert (tmp_path/"4FMT"/"2026"/"Sales_Pitch.pdf").is_file()   # landed next to the real source
    assert "used '4FMT/2026/Sales_Pitch.md'" in r.result["note"]

def test_pdf_nearmiss_ambiguous_lists(tmp_path, monkeypatch):
    for d in ("4FMT","5FMO"):
        (tmp_path/d/"2026").mkdir(parents=True); (tmp_path/d/"2026"/"Student_Overview.md").write_text("x")
    page=_Page(); _wire(monkeypatch, page)
    r=_run({"input":"wrong/Student_Overview.md"}, tmp_path)
    assert r.status=="error" and "exists at:" in r.error and "4FMT/2026/Student_Overview.md" in r.error

def test_pdf_nearmiss_none_is_helpful(tmp_path, monkeypatch):
    page=_Page(); _wire(monkeypatch, page)
    r=_run({"input":"Nope.md"}, tmp_path)
    assert r.status=="error" and "fs.find" in r.error


class _FakeRoute:
    def __init__(self, url):
        class _Req: pass
        self.request = _Req(); self.request.url = url
        self.action = None
    async def continue_(self): self.action = "continue"
    async def abort(self): self.action = "abort"


def test_block_network_route_handler():
    """Rendered documents must be self-contained: only data:/about:/blob: pass."""
    def act(url):
        route = _FakeRoute(url)
        asyncio.run(_block_network(route))
        return route.action
    assert act("data:image/png;base64,iVBOR") == "continue"   # inlined images render
    assert act("about:blank") == "continue"
    assert act("blob:https://x.example/1") == "continue"
    assert act("http://127.0.0.1:4000/beacon") == "abort"      # loopback beacon
    assert act("https://evil.example/beacon.png") == "abort"   # remote beacon
    assert act("file:///etc/passwd") == "abort"


def test_render_registers_network_block(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("x")
    page = _Page()
    captured = {}
    async def fake_get_browser(cfg):
        b = _Browser(page); captured["browser"] = b; return b
    monkeypatch.setattr(M.session, "get_browser", fake_get_browser)
    monkeypatch.setattr(M.session, "LOCK", asyncio.Lock())
    r = _run({"input": "a.md"}, tmp_path)
    assert r.status == "ok"
    assert ("**/*", _block_network) in captured["browser"].last_ctx.routes
