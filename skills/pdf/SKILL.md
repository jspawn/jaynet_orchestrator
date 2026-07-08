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

## Mass extraction — a whole folder of PDFs (ONE job)

For many PDFs, do **not** spawn a sub-agent per file and do **not** hand-loop
`ocr_pdf.py`. Run `pdf_batch.py` **once**: it walks a folder recursively, extracts
every `*.pdf` to markdown (the same text-first + OCR-fallback engine as `ocr_pdf.py`,
with the OCR model loaded a single time for the whole run), and writes one `.md`
per PDF:

    job.start(name="pdf-batch", command=
      "/tmp/pdfenv/bin/python <files['pdf_batch.py']> '<input-folder>' --out '<target-folder>'")

Then `job.wait(<id>)` and `job.logs(<id>)` for the summary (converted / OCR'd /
failed, with per-file errors). Output options:
- no `--out` → `<name>.md` beside each source PDF
- `--out DIR` → all `.md` into DIR (flat; name clashes get a parent-folder prefix)
- `--out DIR --mirror` → recreate the source subfolder tree under DIR

It is **idempotent** — an `.md` newer than its PDF is skipped, so re-runs only pick
up new/changed files (pass `--overwrite` to force). Same `--dpi` / `--ocr` flags as
`ocr_pdf.py`; one failed PDF is logged and skipped, never fatal. This is the route
for "convert all these PDFs to text" — one deterministic job, no model calls.

> **pandoc cannot read PDF.** PDF is not one of pandoc's *input* formats (it only
> *writes* PDF, via LaTeX), so there is no pandoc PDF→markdown path to reach for —
> `pdf_batch.py` / `ocr_pdf.py` are the extraction route.

## Notes

- Report page count and whether pages were OCR'd; PDFs vary wildly.
- Tables / multi-column layouts extract messily; for those, raise `--dpi` or note
  that layout fidelity is limited.
- Don't fabricate content for pages that came back empty — say they need a higher
  DPI or are genuinely blank.

## Creating a PDF

**Use the `pdf.create` tool — one call, no venv, no pip.** It renders your
Markdown (or HTML) through the same headless Chromium the browser tools use, so
tables, CSS, code blocks and page breaks come out correct. This is the reliable
path; do NOT shell out to `xhtml2pdf`, `weasyprint`, or `pandoc` (they're not in
the sandbox and choke on tables).

1. `fs.write` your content as Markdown into the workspace (e.g. `report.md`).
2. `pdf.create(input="report.md")` → writes `report.pdf` into the workspace
   (optional: `output=`, `title=`, `format="A4"|"Letter"|"Legal"`).
3. The PDF is now in the file manager (downloadable there); or
   `deliver.files(["report.pdf"])` to hand it to the user directly.

To convert several files, call `pdf.create` once per file — each is a single,
fast, self-contained call (no jobs, no shared venv, no path juggling).

The old `write_pdf.py` (markdown + xhtml2pdf in a venv) remains in the skill only
as a last resort if the browser session is unavailable; it can't render tables
well, so prefer `pdf.create`. For a Word-quality document, use the **docx** skill.
