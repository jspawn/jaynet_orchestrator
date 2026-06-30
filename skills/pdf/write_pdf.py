#!/usr/bin/env python3
"""Create a PDF from Markdown.  No system deps (markdown + xhtml2pdf).

Usage:  write_pdf.py <input.md> <output.pdf>      (or - for stdin)
Venv:   pip install markdown xhtml2pdf

Renders Markdown -> HTML -> PDF with xhtml2pdf (pure-Python; no LaTeX, no
wkhtmltopdf, no LibreOffice). Supports headings, paragraphs, **bold**/*italic*,
lists, fenced code, blockquotes and pipe tables. For pixel-perfect layout a
LaTeX/HTML-engine path is better, but this needs zero system packages.
"""
import sys

CSS = """
@page { size: A4; margin: 2cm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.4; color: #1a1a1a; }
h1 { font-size: 20pt; color: #0b3d62; margin: 0 0 8pt; }
h2 { font-size: 15pt; color: #0b3d62; margin: 14pt 0 6pt; }
h3 { font-size: 12.5pt; margin: 12pt 0 4pt; }
code, pre { font-family: Courier, monospace; font-size: 9.5pt; background: #f3f4f6; }
pre { padding: 6pt; border: 1px solid #e5e7eb; }
blockquote { color: #555; border-left: 3px solid #cbd5e1; margin: 6pt 0; padding: 2pt 10pt; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #cbd5e1; padding: 4pt 6pt; text-align: left; }
th { background: #eef2f7; }
"""


def main(src, out):
    import markdown
    from xhtml2pdf import pisa
    md = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
    body = markdown.markdown(
        md, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    html = f"<html><head><style>{CSS}</style></head><body>{body}</body></html>"
    with open(out, "wb") as f:
        result = pisa.CreatePDF(html, dest=f)
    if result.err:
        sys.exit(f"PDF generation reported {result.err} error(s)")
    print(f"wrote {out}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: write_pdf.py <input.md|-> <output.pdf>")
    main(sys.argv[1], sys.argv[2])
