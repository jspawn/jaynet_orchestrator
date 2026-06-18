#!/usr/bin/env python3
"""Extract text from a PDF using pypdf. Usage: read_pdf.py <file.pdf>

Run in a venv that has pypdf (pip install pypdf). If the PDF is scanned (image
pages), pypdf returns little/no text — use OCR instead (see the pdf SKILL.md).
"""

import sys


def main(path: str) -> None:
    try:
        from pypdf import PdfReader
    except ImportError:
        print("pypdf not installed. In a venv: pip install pypdf", file=sys.stderr)
        sys.exit(3)
    reader = PdfReader(path)
    pages, body = [], []
    for i, page in enumerate(reader.pages, 1):
        txt = page.extract_text() or ""
        body.append(txt)
        pages.append(f"# Page {i}\n{txt}")
    if not "".join(body).strip():
        sys.stderr.write("warning: no extractable text — likely a scanned PDF; use OCR.\n")
    print("\n\n".join(pages))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: read_pdf.py <file.pdf>", file=sys.stderr); sys.exit(2)
    main(sys.argv[1])
