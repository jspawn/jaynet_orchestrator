---
name: docx
description: Read, extract from, or CREATE Microsoft Word (.docx) files. Load when a .docx is uploaded/referenced and you need its contents, or when the user asks to create/write/generate a Word document.
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

## Creating a .docx

This skill bundles `write_docx.py`, which turns **Markdown into a Word document**
(headings, **bold**/*italic*/`code`, bullet & numbered lists, `>` quotes, fenced
code, `---` rules, and pipe tables). It needs `python-docx` (pip, no system deps).

1. Write your content as Markdown into your **workspace** with `fs.write`
   (e.g. `report.md`) — don't inline a multi-line program in `bash -lc`.
2. Generate the file in a venv via **job.start**, writing the .docx into your
   workspace too:

       job.start(name="make-docx", command=
         "bash -lc 'test -d /tmp/docenv || python -m venv /tmp/docenv; "
         "/tmp/docenv/bin/pip -q install python-docx && "
         "/tmp/docenv/bin/python <files['write_docx.py']> report.md report.docx'")

3. `job.wait(<id>)` for it to finish, then **`deliver.files(["report.docx"])`**
   to hand the document to the user as a download.

For layouts beyond what Markdown expresses (custom styles, headers/footers,
images, precise tables), write your own short generator with `python-docx`
(`fs.write` a `.py` into the workspace, run it the same way) — `write_docx.py` is
a readable starting point to copy from.
