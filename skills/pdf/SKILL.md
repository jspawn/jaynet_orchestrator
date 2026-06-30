---
name: pdf
description: Read/OCR existing PDF files, or CREATE new PDFs. Load when a .pdf is uploaded/referenced and you need its contents, or when the user asks to create/write/generate a PDF.
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

## Creating a PDF

This skill bundles `write_pdf.py`, which renders **Markdown → PDF** via
`markdown` + `xhtml2pdf` (pure-Python: no LaTeX, wkhtmltopdf, or LibreOffice).
Supports headings, **bold**/*italic*, lists, fenced code, blockquotes and tables.

1. `fs.write` your content as Markdown into the workspace (e.g. `report.md`).
2. Generate via **job.start** into the workspace:

       job.start(name="make-pdf", command=
         "bash -lc 'test -d /tmp/docenv || python -m venv /tmp/docenv; "
         "/tmp/docenv/bin/pip -q install markdown xhtml2pdf && "
         "/tmp/docenv/bin/python <files['write_pdf.py']> report.md report.pdf'")

3. `job.wait(<id>)`, then **`deliver.files(["report.pdf"])`**.

The styling lives in a CSS block at the top of `write_pdf.py` — copy it into a
workspace script and tweak fonts/margins/colors for a custom look. For precise
typesetting (multi-column, exact layout) a LaTeX path is better, but this needs
zero system packages. To make a Word-quality document instead, use the **docx**
skill; to produce a PDF *from* a .docx you'd need LibreOffice (a system package) —
generate the PDF directly from Markdown here instead.
