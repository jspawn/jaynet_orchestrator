---
name: image
description: Get information out of an image — OCR text from a screenshot or scan. Load when an image is uploaded and you need the text in it.
---
# Reading images

The local brain can't see images. There are two needs; be clear which one applies.

## Text in the image (OCR) — the working path

For screenshots, scans, or photos of documents, extract the text with `tesseract`
via **code.run** — synchronous, the text comes straight back in the result:

    code.run(command="tesseract <path-to-image> stdout")

(Needs `tesseract` — shipped in the devbox toolchain container and common on
the host. If it's missing, say so rather than guessing the content.) For
multi-language or better accuracy, `tesseract <img> stdout -l eng+deu --psm 6`.

## Understanding the picture (not just text) — needs a vision model

Describing *what an image depicts* (not OCR) requires a multimodal model.
**`llm.call` is currently text-only**, so it can't do this as-is. Don't pretend to
see the image. Options to tell the user:
- Add image support to `llm.call` (build a multimodal `content` array with a
  base64 image block for the Claude/Gemini aliases) — a small enhancement.
- Or, if it's really text you need, use OCR above.

## Notes

- Never describe or transcribe an image you haven't actually run through OCR (or a
  real vision model) — report what the tool returned, and say if it returned
  nothing.
- The uploaded image is also viewable in the chat UI; the user can see it even
  when you can't.
