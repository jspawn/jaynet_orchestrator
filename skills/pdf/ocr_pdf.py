#!/usr/bin/env python3
"""Read ANY PDF — text-based, scanned, or mixed — with NO system dependencies.

For each page: extract the text layer with pypdfium2; if the page has (almost) no
text, it's a scanned image, so render it and OCR with rapidocr-onnxruntime (CPU,
ONNX — no tesseract/poppler/ocrmypdf needed). Prints one block per page.

Usage:  read_any_pdf.py <file.pdf> [--dpi N] [--pages 1-3,5] [--ocr auto|always|never]
Venv:   pip install pypdfium2 rapidocr-onnxruntime
"""
import argparse, sys

MIN_TEXT = 12  # chars; below this a page is treated as scanned and OCR'd


def _pages(spec, n):
    if not spec:
        return range(n)
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out += range(int(a) - 1, int(b))
        elif part:
            out.append(int(part) - 1)
    return [i for i in out if 0 <= i < n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--dpi", type=int, default=220)
    ap.add_argument("--pages", default=None, help="e.g. 1-3,5 (1-based)")
    ap.add_argument("--ocr", choices=["auto", "always", "never"], default="auto")
    a = ap.parse_args()
    try:
        import pypdfium2 as pdfium
    except ImportError as e:
        sys.exit(f"missing dep: {e}. In a venv: pip install pypdfium2 rapidocr-onnxruntime")

    pdf = pdfium.PdfDocument(a.pdf)
    n = len(pdf)
    ocr = None  # lazily constructed only if a page needs it
    scale = a.dpi / 72.0
    ocr_pages = []
    for i in _pages(a.pages, n):
        page = pdf[i]
        text = ""
        if a.ocr != "always":
            text = (page.get_textpage().get_text_range() or "").strip()
        need_ocr = a.ocr == "always" or (a.ocr == "auto" and len(text) < MIN_TEXT)
        if need_ocr:
            try:
                import numpy as np
                if ocr is None:
                    from rapidocr_onnxruntime import RapidOCR
                    ocr = RapidOCR()
                pil = page.render(scale=scale).to_pil().convert("RGB")
                res, _ = ocr(np.array(pil))
                text = "\n".join(line[1] for line in res) if res else ""
                ocr_pages.append(i + 1)
            except ImportError as e:
                sys.exit(f"OCR needed but unavailable: {e}. pip install rapidocr-onnxruntime")
        print(f"# Page {i + 1}{' (OCR)' if (i + 1) in ocr_pages else ''}\n{text}\n")
    if ocr_pages:
        sys.stderr.write(f"OCR'd scanned pages: {ocr_pages}\n")


if __name__ == "__main__":
    main()
