#!/usr/bin/env python3
"""Create a PowerPoint .pptx from Markdown.  No system deps (python-pptx).

Usage:  write_pptx.py <input.md> <output.pptx>      (or - for stdin)
Venv:   pip install python-pptx

Slide model: slides are separated by a `---` line, OR (if there are none) each
`#`/`##` heading starts a new slide. Within a slide the first heading is the
title; `- ` / `* ` lines become bullets (indent = sub-bullets); other non-empty
lines become body text.
"""
import re, sys


def _split_slides(md):
    lines = md.splitlines()
    if any(re.match(r"^\s*---\s*$", ln) for ln in lines):
        chunks, cur = [], []
        for ln in lines:
            if re.match(r"^\s*---\s*$", ln):
                chunks.append(cur); cur = []
            else:
                cur.append(ln)
        chunks.append(cur)
        return [c for c in chunks if any(x.strip() for x in c)]
    # else split on headings
    chunks, cur = [], []
    for ln in lines:
        if re.match(r"^#{1,2}\s+", ln) and cur:
            chunks.append(cur); cur = [ln]
        else:
            cur.append(ln)
    if cur:
        chunks.append(cur)
    return [c for c in chunks if any(x.strip() for x in c)]


def main(src, out):
    from pptx import Presentation
    from pptx.util import Pt
    md = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
    prs = Presentation()
    blank, bullet_layout = prs.slide_layouts[6], prs.slide_layouts[1]
    for chunk in _split_slides(md):
        title, body = None, []
        for ln in chunk:
            m = re.match(r"^(#{1,6})\s+(.*)", ln)
            if m and title is None:
                title = m.group(2).strip(); continue
            if ln.strip() == "":
                continue
            indent = 1 if re.match(r"^\s{2,}[-*+]\s", ln) else 0
            text = re.sub(r"^\s*[-*+]\s+", "", ln).strip()
            text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)   # drop md emphasis markers
            text = re.sub(r"`(.+?)`", r"\1", text)
            body.append((indent, text))
        slide = prs.slides.add_slide(bullet_layout)
        slide.shapes.title.text = title or "Slide"
        tf = slide.placeholders[1].text_frame; tf.clear()
        for j, (indent, text) in enumerate(body):
            p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            p.text = text; p.level = indent; p.font.size = Pt(20 if indent == 0 else 18)
    prs.save(out)
    print(f"wrote {out} ({len(prs.slides._sldIdLst)} slides)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: write_pptx.py <input.md|-> <output.pptx>")
    main(sys.argv[1], sys.argv[2])
