#!/usr/bin/env python3
"""Read ANY PDF — text-based, scanned, or mixed — with NO system dependencies.

For each page: extract the text layer with pypdfium2; if the page has (almost) no
text, it's a scanned image, so render it and OCR with rapidocr-onnxruntime (CPU,
ONNX — no tesseract/poppler/ocrmypdf needed). Prints one block per page.

Usage:  ocr_pdf.py <file.pdf> [--dpi N] [--pages 1-3,5] [--ocr auto|always|never]
Venv:   pip install pypdfium2 rapidocr-onnxruntime

Importable:  from ocr_pdf import extract_pdf
             md, ocr_pages = extract_pdf("f.pdf")     # text-first, OCR fallback
The OCR engine is a lazy process-wide singleton, so extract_pdf() called in a loop
(see pdf_batch.py) loads the ONNX model only once for the whole run.
"""
import argparse
import sys

MIN_TEXT = 12  # chars; below this a page is treated as scanned and OCR'd

_OCR = None


def _get_ocr():
    """Build and cache one RapidOCR engine for the whole process."""
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


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


def extract_pdf(path, dpi=220, pages=None, ocr="auto"):
    """Extract a PDF to markdown (one '# Page N' block per page).

    Returns (markdown, ocr_pages). Text layer via pypdfium2; a page with < MIN_TEXT
    chars is rendered at `dpi` and OCR'd when ocr='auto', always OCR'd for 'always',
    or left as its (sparse) text for 'never'. Raises ImportError if a lib is missing.
    """
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    try:
        n = len(pdf)
        scale = dpi / 72.0
        ocr_pages = []
        blocks = []
        for i in _pages(pages, n):
            page = pdf[i]
            text = ""
            if ocr != "always":
                text = (page.get_textpage().get_text_range() or "").strip()
            need_ocr = ocr == "always" or (ocr == "auto" and len(text) < MIN_TEXT)
            if need_ocr:
                import numpy as np
                engine = _get_ocr()
                pil = page.render(scale=scale).to_pil().convert("RGB")
                res, _ = engine(np.array(pil))
                text = "\n".join(line[1] for line in res) if res else ""
                ocr_pages.append(i + 1)
            tag = " (OCR)" if (i + 1) in ocr_pages else ""
            blocks.append(f"# Page {i + 1}{tag}\n{text}\n")
        return "\n".join(blocks), ocr_pages
    finally:
        pdf.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--dpi", type=int, default=220)
    ap.add_argument("--pages", default=None, help="e.g. 1-3,5 (1-based)")
    ap.add_argument("--ocr", choices=["auto", "always", "never"], default="auto")
    a = ap.parse_args()
    try:
        md, ocr_pages = extract_pdf(a.pdf, dpi=a.dpi, pages=a.pages, ocr=a.ocr)
    except ImportError as e:
        sys.exit(f"missing dep: {e}. In a venv: pip install pypdfium2 rapidocr-onnxruntime")
    print(md)
    if ocr_pages:
        sys.stderr.write(f"OCR'd scanned pages: {ocr_pages}\n")


if __name__ == "__main__":
    main()
