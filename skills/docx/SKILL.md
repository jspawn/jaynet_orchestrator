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
this skill. Run it on the uploaded file with **code.run** — synchronous, so the
text comes straight back in the result, no polling:

    code.run(command="python3 <files['read_docx.py']> <path-to-the-file.docx>")

It prints the document text (one paragraph per line) to stdout.

(The `.docx` path is the one given to you in the "[Attached files]" note when the
user uploaded it.)

## Inline alternative

If `tools.code.allowed_imports` includes `zipfile`, `xml` and `zlib`, you can run
the same extraction inline with `code.execute` instead. By default the
sandbox does **not** allow those modules, so prefer the code.run route above
unless you know they're enabled.

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
2. Generate the file in a venv via **code.run** (needs `network: true` once,
   for pip), writing the .docx into your workspace too:

       code.run(network=true, timeout=180, command=
         "bash -lc 'test -d /tmp/docenv || python3 -m venv /tmp/docenv; "
         "/tmp/docenv/bin/pip -q install python-docx && "
         "/tmp/docenv/bin/python <files['write_docx.py']> report.md report.docx'")

3. The .docx is there when the call returns — **`deliver.files(["report.docx"])`**
   to hand the document to the user as a download.

For layouts beyond what Markdown expresses (custom styles, headers/footers,
images, precise tables), write your own short generator with `python-docx`
(`fs.write` a `.py` into the workspace, run it the same way) — `write_docx.py` is
a readable starting point to copy from.
