#!/usr/bin/env python3
"""Create a Word .docx from Markdown.  No system deps (python-docx).

Usage:  write_docx.py <input.md> <output.docx>        # or read MD from stdin:
        write_docx.py - <output.docx> < notes.md
Venv:   pip install python-docx

Supports: # headings (1-6), paragraphs, **bold** *italic* `code` inline,
- / * bullet lists, 1. numbered lists, > blockquotes, ``` fenced code,
--- horizontal rules, and | pipe | tables |.
"""
import re, sys


def _runs(p, text):
    # inline **bold**, *italic*/_italic_, `code`
    for tok in re.split(r"(\*\*.+?\*\*|\*.+?\*|_.+?_|`.+?`)", text):
        if not tok:
            continue
        if tok.startswith("**") and tok.endswith("**"):
            p.add_run(tok[2:-2]).bold = True
        elif (tok.startswith("*") and tok.endswith("*")) or (tok.startswith("_") and tok.endswith("_")):
            p.add_run(tok[1:-1]).italic = True
        elif tok.startswith("`") and tok.endswith("`"):
            r = p.add_run(tok[1:-1]); r.font.name = "Consolas"
        else:
            p.add_run(tok)


def main(src, out):
    from docx import Document
    from docx.shared import Pt
    md = (sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read())
    doc = Document()
    lines = md.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.strip().startswith("```"):                      # fenced code
            i += 1; buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i]); i += 1
            p = doc.add_paragraph(); r = p.add_run("\n".join(buf))
            r.font.name = "Consolas"; r.font.size = Pt(9)
            i += 1; continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            doc.add_heading(m.group(2).strip(), level=min(len(m.group(1)), 6)); i += 1; continue
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):            # --- hr
            doc.add_paragraph().add_run("_" * 40); i += 1; continue
        if line.strip().startswith(">"):                        # blockquote
            p = doc.add_paragraph(style="Intense Quote"); _runs(p, line.strip()[1:].strip()); i += 1; continue
        if line.lstrip().startswith(("- ", "* ", "+ ")):        # bullets
            p = doc.add_paragraph(style="List Bullet"); _runs(p, line.lstrip()[2:]); i += 1; continue
        if re.match(r"^\s*\d+\.\s+", line):                     # numbered
            p = doc.add_paragraph(style="List Number"); _runs(p, re.sub(r"^\s*\d+\.\s+", "", line)); i += 1; continue
        if line.strip().startswith("|") and "|" in line.strip()[1:]:   # table
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not re.match(r"^[\s:|-]+$", lines[i]):       # skip --- separator row
                    rows.append(cells)
                i += 1
            if rows:
                t = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
                t.style = "Light Grid Accent 1"
                for ri, r in enumerate(rows):
                    for ci, c in enumerate(r):
                        _runs(t.cell(ri, ci).paragraphs[0], c)
            continue
        if line.strip() == "":
            i += 1; continue
        p = doc.add_paragraph(); _runs(p, line); i += 1            # paragraph
    doc.save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: write_docx.py <input.md|-> <output.docx>")
    main(sys.argv[1], sys.argv[2])
