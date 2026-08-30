---
name: pptx
description: Extract slide text from, or CREATE, PowerPoint .pptx files. Load when a .pptx is uploaded/referenced and you need its text, or when the user asks to create/write/generate a slide deck or presentation.
---
# Reading PowerPoint (.pptx) files

A `.pptx` is a ZIP of XML; each slide's text lives in `ppt/slides/slideN.xml` as
DrawingML `<a:t>` runs. The bundled `read_pptx.py` is standard-library only.

## Run it

    job.start(name="read-pptx",
              command="python <files['read_pptx.py']> <path-to-file.pptx>")

It prints one `# Slide N` block per slide with that slide's text. Read it back
with `job.logs`. (Or run inline with `code.run` (language=python) if `zipfile`/`xml`/`zlib` are
in `tools.code.allowed_imports`.)

## Notes

- Extracts visible text from shapes/placeholders in slide order. Speaker notes
  live separately in `ppt/notesSlides/` — mention if the user needs those.
- Reading order within a slide follows the XML, which isn't always the visual
  top-to-bottom order; don't over-trust ordering for layout-heavy decks.
- Images, charts, and embedded objects aren't text — note their presence rather
  than inventing captions. For full fidelity use `python-pptx` in a venv.

## Creating a .pptx

This skill bundles `write_pptx.py`, which turns **Markdown into slides**. It needs
`python-pptx` (pip, no system deps). Slide model: slides are separated by a `---`
line (or, with none, each `#`/`##` heading starts a slide); the first heading is
the slide title and `- ` lines become bullets (indent → sub-bullets).

1. `fs.write` your deck as Markdown into the workspace (e.g. `deck.md`).
2. Generate via **job.start** into the workspace:

       job.start(name="make-pptx", command=
         "bash -lc 'test -d /tmp/docenv || python -m venv /tmp/docenv; "
         "/tmp/docenv/bin/pip -q install python-pptx && "
         "/tmp/docenv/bin/python <files['write_pptx.py']> deck.md deck.pptx'")

3. `job.wait(<id>)`, then **`deliver.files(["deck.pptx"])`**.

For images, custom layouts, charts, or themed templates, write a short
`python-pptx` script (`fs.write` a `.py`, run it the same way); `write_pptx.py`
is a starting point. To start from a corporate template, open a `.potx`/`.pptx`
with `Presentation("template.pptx")` and add slides to it.
