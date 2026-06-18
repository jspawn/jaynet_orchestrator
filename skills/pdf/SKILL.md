---
name: pdf
description: Read or extract text from PDF files, including scanned PDFs via OCR. Load when a .pdf is uploaded or referenced and you need its contents.
---
# Reading PDF files

There is no standard-library PDF parser, so this runs in a venv via **job.start**
(full environment). The uploaded file's path is in the "[Attached files]" note.

## Text-based PDFs (most documents)

This skill bundles `read_pdf.py` (uses `pypdf`). Its absolute path is in the
`files` map from when you loaded this skill.

    job.start(name="read-pdf", command=
      "bash -lc 'python -m venv /tmp/pdfenv 2>/dev/null; "
      "/tmp/pdfenv/bin/pip -q install pypdf && "
      "/tmp/pdfenv/bin/python <files['read_pdf.py']> <path-to-file.pdf>'")

Then `job.status` to wait and `job.logs(<id>)` to read the extracted text (one
block per page). If a `pdftotext` binary is available on the box, `pdftotext
-layout <file.pdf> -` is faster and preserves layout — try it first if you prefer.

## Scanned PDFs (image pages)

If `read_pdf.py` prints little/no text, the pages are images — extract with OCR:

    job.start(name="ocr-pdf", command=
      "bash -lc 'ocrmypdf --sidecar /tmp/out.txt --skip-text <file.pdf> /tmp/ocr.pdf "
      "&& cat /tmp/out.txt'")

(needs `ocrmypdf` + `tesseract`). If those aren't installed, tell the user what's
needed rather than guessing at the content.

## Notes

- Report page count and whether extraction looked clean; PDFs vary wildly.
- Tables and multi-column layouts often extract messily — for those, `pdfplumber`
  (in the venv) does a better job; mention it if layout fidelity matters.
- Don't fabricate content for pages that extracted empty — say they need OCR.
