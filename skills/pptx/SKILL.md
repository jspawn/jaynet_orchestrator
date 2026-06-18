---
name: pptx
description: Extract slide text from PowerPoint .pptx files. Load when a .pptx is uploaded or referenced and you need its text.
---
# Reading PowerPoint (.pptx) files

A `.pptx` is a ZIP of XML; each slide's text lives in `ppt/slides/slideN.xml` as
DrawingML `<a:t>` runs. The bundled `read_pptx.py` is standard-library only.

## Run it

    job.start(name="read-pptx",
              command="python <files['read_pptx.py']> <path-to-file.pptx>")

It prints one `# Slide N` block per slide with that slide's text. Read it back
with `job.logs`. (Or run inline with `code.execute` if `zipfile`/`xml`/`zlib` are
in `tools.code.allowed_imports`.)

## Notes

- Extracts visible text from shapes/placeholders in slide order. Speaker notes
  live separately in `ppt/notesSlides/` — mention if the user needs those.
- Reading order within a slide follows the XML, which isn't always the visual
  top-to-bottom order; don't over-trust ordering for layout-heavy decks.
- Images, charts, and embedded objects aren't text — note their presence rather
  than inventing captions. For full fidelity use `python-pptx` in a venv.
