---
name: docx
description: Read or extract text from Microsoft Word (.docx) files. Load when a .docx is uploaded or referenced and you need its contents.
---
# Reading Word (.docx) files

A `.docx` is a ZIP archive of XML; the body text lives in `word/document.xml`. The
local brain can't read the binary directly, so extract the text first.

## Quickest path — the bundled script

This skill bundles `read_docx.py` (standard-library only: `zipfile` +
`ElementTree`). Its absolute path is in the `files` map returned when you loaded
this skill. Run it on the uploaded file with **job.start**, which has the full
Python environment:

    job.start(command="python <files['read_docx.py']> <path-to-the-file.docx>",
              name="read-docx")

It prints the document text (one paragraph per line) to stdout. When the job
finishes, read it back with `job.logs(<job_id>)`. For a normal document this is
near-instant — poll once with `job.status` and then fetch the logs.

(The `.docx` path is the one given to you in the "[Attached files]" note when the
user uploaded it.)

## Inline alternative

If `tools.code.allowed_imports` includes `zipfile`, `xml` and `zlib`, you can run
the same extraction inline with `code.execute` instead of a job. By default the
sandbox does **not** allow those modules, so prefer the job route above unless you
know they're enabled.

## What this does and doesn't cover

- Extracts the main body text. Headers, footers, footnotes, and table cells live
  in other parts (`word/header*.xml`, separate table XML) — if those matter for
  the task, say so to the user rather than silently omitting them.
- For high-fidelity conversion (preserving styles, or tables → Markdown), a
  library such as `python-docx` or `markitdown` in a venv does a better job; tell
  the user if they need that level of fidelity and offer to set it up.
