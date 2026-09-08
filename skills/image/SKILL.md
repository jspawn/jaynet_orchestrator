---
name: image
description: Get information out of an image — OCR text from a screenshot or scan, or describe what a picture shows. Load when an image is uploaded and you need the text in it or need to understand what it depicts.
---
# Reading images

There are two needs; be clear which one applies.

## Understanding the picture (and OCR) — llm.call with images

Describing *what an image depicts* — and reading text in it — works via a
multimodal `llm.call`:

    llm.call(task="Describe this image in detail, including any readable text.",
             images=["<path-to-image>"])

Each `images` entry is a workspace path (png/jpg/jpeg/webp/gif/bmp, ≤10MB) or
a data URL. With no explicit `model`, the call routes to the **local-vision**
slot (a CPU llama-server with a vision projector, assigned in Admin →
Presets). You may also pass an explicit cloud `model` for image work — that
sends the image off-box, same privacy gate as any cloud llm.call.

If the vision slot isn't running or assigned, the call returns a clear error
saying so — don't retry blindly; tell the user, or fall back to OCR below if
it's really text you need.

## Text in the image (OCR) — the always-available fallback

For screenshots, scans, or photos of documents, extract the text with
`tesseract` via **code.run** — synchronous, the text comes straight back in
the result:

    code.run(command="tesseract <path-to-image> stdout")

(Needs `tesseract` — shipped in the devbox toolchain container and common on
the host. If it's missing, say so rather than guessing the content.) For
multi-language or better accuracy, `tesseract <img> stdout -l eng+deu --psm 6`.
Use this when no vision slot is running, or when you only need the text and a
cheap local pass beats a model call.

## Notes

- Never describe or transcribe an image you haven't actually run through OCR
  or a real vision model — report what the tool returned, and say if it
  returned nothing.
- If the analysis is uncertain or partial, still produce your best-effort
  answer (and the requested output file, if any) and state the uncertainty —
  an imperfect deliverable beats none.
- The uploaded image is also viewable in the chat UI; the user can see it even
  when you can't.
