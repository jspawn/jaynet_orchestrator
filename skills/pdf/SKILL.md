---
name: pdf
description: Read or extract text from PDF files, including scanned PDFs via OCR. Load when a .pdf is uploaded or referenced and you need its contents.
---
# Reading PDF files

There is no standard-library PDF parser, so this runs in a venv via **job.start**
(full environment). The uploaded file's path is in the "[Attached files]" note.
This skill bundles two scripts; their absolute paths are in the `files` map from
when you loaded the skill — run those **files** directly.

> **Do not inline Python inside `bash -lc '…'`** — nested quotes will break the
> job (this is the usual cause of "ended without exit code" / quoting errors).
> Run a bundled script with the PDF path as an argument, or, if you need custom
> logic, `fs.write` a `.py` file into your workspace and run that file. Never
> paste a multi-line program into the command string.

## One venv for everything

Create it once; reuse it across calls (skip install if it already exists):

    job.start(name="pdfenv", command=
      "bash -lc 'test -d /tmp/pdfenv || python -m venv /tmp/pdfenv; "
      "/tmp/pdfenv/bin/pip -q install pypdf pypdfium2 rapidocr-onnxruntime'")

`pypdf` = quick text extraction; `pypdfium2` = render + text; `rapidocr-onnxruntime`
= OCR (CPU/ONNX). **None of these need system packages** — no tesseract, poppler,
ocrmypdf, or pdftotext. (If `tesseract`/`pdftotext` happen to be installed they're
faster, but assume they are NOT and use the bundled scripts.)

## Any PDF — text, scanned, or mixed (preferred)

`ocr_pdf.py` reads each page's text layer and **automatically OCRs only the pages
that have none** (scanned images). This one call handles every case:

    job.start(name="read-pdf", command=
      "/tmp/pdfenv/bin/python <files['ocr_pdf.py']> <path-to-file.pdf>")

Then `job.wait(<id>)` to block until it finishes, and `job.logs(<id>)` to read it —
one block per page,
pages that were OCR'd are tagged `(OCR)`. Options: `--pages 1-3,5` (1-based),
`--dpi 300` (raise if OCR is sloppy; 220 is the default), `--ocr always|never`.
First run constructs the OCR models (a few seconds); subsequent pages are fast.

## Quick text-only path (light, optional)

If you know the PDF is digital-native (not scanned) and just want speed with the
smallest install, `read_pdf.py` (pypdf only) prints text per page:

    job.start(name="read-pdf", command=
      "/tmp/pdfenv/bin/python <files['read_pdf.py']> <path-to-file.pdf>")

If it prints little/no text, the pages are scanned — switch to `ocr_pdf.py` above.

## Notes

- Report page count and whether pages were OCR'd; PDFs vary wildly.
- Tables / multi-column layouts extract messily; for those, raise `--dpi` or note
  that layout fidelity is limited.
- Don't fabricate content for pages that came back empty — say they need a higher
  DPI or are genuinely blank.
