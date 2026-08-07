"""pdf.create — turn a Markdown (or HTML) file into a PDF, reliably.

Replaces the fragile skills/pdf/write_pdf.py path (venv + markdown + xhtml2pdf,
which can't render tables). This renders through the SAME headless Chromium the
browser.* tools already use — Chrome's page.pdf() handles tables, CSS, code
blocks and page breaks correctly, and needs no Python PDF libraries, no venv,
and no pip install. One tool call: no jobs, no path/quoting fumbling.
"""

from __future__ import annotations

import re
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult, work_roots
from tools.browser import session

_CSS = """
@page { size: __SIZE__; margin: 1.8cm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.45; color: #1a1a1a; }
h1 { font-size: 21pt; color: #0b3d62; margin: 0 0 10pt; }
h2 { font-size: 15pt; color: #0b3d62; margin: 16pt 0 6pt; border-bottom: 1px solid #e5e7eb; padding-bottom: 3pt; }
h3 { font-size: 12.5pt; margin: 12pt 0 4pt; }
p { margin: 6pt 0; }
a { color: #0b57d0; text-decoration: none; }
code { font-family: "DejaVu Sans Mono", Consolas, monospace; font-size: 9.5pt; background: #f3f4f6; padding: 1pt 3pt; border-radius: 3px; }
pre { background: #f6f8fa; border: 1px solid #e5e7eb; border-radius: 6px; padding: 8pt 10pt; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { color: #555; border-left: 3px solid #cbd5e1; margin: 8pt 0; padding: 2pt 12pt; }
ul, ol { margin: 6pt 0 6pt 18pt; }
li { margin: 2pt 0; }
hr { border: 0; border-top: 1px solid #e5e7eb; margin: 12pt 0; }
img { max-width: 100%; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0; font-size: 10pt; }
th, td { border: 1px solid #cbd5e1; padding: 5pt 8pt; text-align: left; vertical-align: top; }
th { background: #eef2f7; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }
"""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_SELF_CONTAINED_SCHEMES = ("data:", "about:", "blob:")


async def _block_network(route):
    """Playwright route handler: the rendered document must be fully
    self-contained. Everything but data:/about:/blob: URLs is aborted, so a
    `![](http://…)` image or `<script src>` can't beacon out to arbitrary
    hosts (incl. loopback) while Chromium renders the page."""
    if route.request.url.startswith(_SELF_CONTAINED_SCHEMES):
        await route.continue_()
    else:
        await route.abort()


def _inline(s: str) -> str:
    # images before links; then bold/italic/code/links on the escaped text
    s = re.sub(r"!\[([^\]]*)\]\((\S+?)\)", r'<img src="\2" alt="\1">', s)
    s = re.sub(r"\[([^\]]+)\]\((\S+?)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def _md_to_html(md: str) -> str:
    """Compact, dependency-free Markdown -> HTML (Chrome does the hard rendering).
    Handles headings, lists, tables, code fences, blockquotes, rules, inline."""
    lines = _esc(md).split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lvl = len(m.group(1)); out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>"); i += 1; continue
        if re.match(r"^\s*(```|~~~)", ln):                       # fenced code
            fence = ln.strip()[:3]; body = []; i += 1
            while i < n and lines[i].strip()[:3] != fence:
                body.append(lines[i]); i += 1
            i += 1
            out.append("<pre><code>" + "\n".join(body) + "</code></pre>"); continue
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", ln):          # hr
            out.append("<hr>"); i += 1; continue
        if re.match(r"^\s*&gt;", ln):                            # blockquote
            buf = []
            while i < n and re.match(r"^\s*&gt;", lines[i]):
                buf.append(re.sub(r"^\s*&gt;\s?", "", lines[i])); i += 1
            out.append("<blockquote>" + _inline("<br>".join(buf)) + "</blockquote>"); continue
        # table: header row + separator
        if "|" in ln and i + 1 < n and re.match(r"^\s*\|?[ :|\-]*-[ :|\-]*\|?\s*$", lines[i + 1]):
            cells = lambda r: [c.strip() for c in r.strip().strip("|").split("|")]
            t = "<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells(ln)) + "</tr></thead><tbody>"
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                t += "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells(lines[i])) + "</tr>"; i += 1
            out.append(t + "</tbody></table>"); continue
        if re.match(r"^\s*[-*+]\s+", ln):                        # ul
            buf = []
            while i < n and re.match(r"^\s*[-*+]\s+", lines[i]):
                buf.append("<li>" + _inline(re.sub(r"^\s*[-*+]\s+", "", lines[i])) + "</li>"); i += 1
            out.append("<ul>" + "".join(buf) + "</ul>"); continue
        if re.match(r"^\s*\d+\.\s+", ln):                        # ol
            buf = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                buf.append("<li>" + _inline(re.sub(r"^\s*\d+\.\s+", "", lines[i])) + "</li>"); i += 1
            out.append("<ol>" + "".join(buf) + "</ol>"); continue
        if not ln.strip():
            i += 1; continue
        para = [ln]; i += 1                                       # paragraph
        while i < n and lines[i].strip() and not re.match(r"^\s*(#{1,6}\s|[-*+]\s|\d+\.\s|&gt;|```|~~~)", lines[i]):
            para.append(lines[i]); i += 1
        out.append("<p>" + _inline("<br>".join(para)) + "</p>")
    return "\n".join(out)


class PdfCreate(Tool):
    name = "pdf.create"
    description = (
        "Create a PDF from a Markdown (or HTML) file and save it to the workspace. "
        "Renders through the same headless Chromium the browser tools use, so tables, "
        "CSS and code blocks come out right — no venv, no pip, no external PDF "
        "libraries. Give the input file path and (optionally) an output name and page "
        "size. This is the reliable way to make a PDF; do NOT shell out to xhtml2pdf / "
        "weasyprint / pandoc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "Path to the .md or .html file in the workspace."},
            "output": {"type": "string", "description": "Output .pdf name/path (default: input name with .pdf)."},
            "title": {"type": "string", "description": "Optional document title (H1 prepended if given)."},
            "format": {"type": "string", "enum": ["A4", "Letter", "Legal"], "description": "Page size (default A4)."},
        },
        "required": ["input"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        roots = work_roots(ctx)
        if not roots:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="no workspace to read from / write to")
        root = Path(roots[0]).resolve()

        def _resolve(rel: str) -> Path | None:
            p = (root / (rel or "").lstrip("/")).resolve()
            return p if (p == root or root in p.parents) else None

        given = (args.get("input") or "").strip()
        src = _resolve(given)
        resolved_note = None
        if src is None or not src.is_file():
            # Near-miss recovery: the model often guesses the wrong directory but the
            # right filename. Look the basename up across the workspace and self-correct
            # (unique match) or report exactly where it lives (ambiguous), instead of
            # just failing and forcing a separate lookup.
            bn = Path(given).name
            matches = sorted(str(p.relative_to(root)) for p in root.rglob(bn)
                             if p.is_file() and ".git" not in p.parts) if bn else []
            if len(matches) == 1:
                src = _resolve(matches[0])
                resolved_note = f"input '{given}' not found; used '{matches[0]}' instead"
            elif len(matches) > 1:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"'{given}' not found; a file named '{bn}' exists at: "
                                        + ", ".join(matches) + " — pass the full path.")
            else:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"'{given}' not found, and no file named '{bn}' exists "
                                        "anywhere in the workspace. Use fs.find or fs.list to locate it.")
        # Where to write: next to the source by default. Honor an explicit `output`
        # only when its parent directory actually exists, so a guessed output dir
        # can't create a stray folder or lose the file. Always ends in .pdf.
        out_arg = (args.get("output") or "").strip()
        if out_arg:
            cand = _resolve(out_arg)
            dst = cand if (cand is not None and cand.parent.is_dir()) else src.with_name(Path(out_arg).name)
        else:
            dst = src.with_name(src.stem + ".pdf")
        if not dst.name.lower().endswith(".pdf"):
            dst = dst.with_name(dst.name + ".pdf")
        if not (dst == root or root in dst.parents):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="invalid output path")
        out_rel = str(dst.relative_to(root))

        raw = src.read_text(encoding="utf-8", errors="replace")
        if src.suffix.lower() in (".html", ".htm"):
            html = raw if "<html" in raw.lower() else f"<html><body>{raw}</body></html>"
        else:
            title = (args.get("title") or "").strip()
            body = (f"<h1>{_esc(title)}</h1>\n" if title else "") + _md_to_html(raw)
            css = _CSS.replace("__SIZE__", args.get("format", "A4"))
            html = f'<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body>{body}</body></html>'

        bcfg = (ctx.config.get("tools", {}) or {}).get("browser", {}) or {}
        fmt = args.get("format", "A4")
        try:
            async with session.LOCK:
                browser = await session.get_browser(bcfg)
                context = await browser.new_context()
                try:
                    await context.route("**/*", _block_network)
                    page = await context.new_page()
                    await page.set_content(html, wait_until="load")
                    data = await page.pdf(format=fmt, print_background=True)
                finally:
                    await context.close()
        except RuntimeError as e:                 # playwright/browser not available
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"headless browser unavailable: {e}")
        except Exception as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"PDF render failed: {type(e).__name__}: {e}")

        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        note = f"wrote {out_rel} ({len(data)} bytes) — visible in the file manager and downloadable"
        if resolved_note:
            note = resolved_note + "; " + note
        return ToolResult(status="ok", tool_name=self.name, result={
            "path": out_rel, "size": len(data), "note": note})
