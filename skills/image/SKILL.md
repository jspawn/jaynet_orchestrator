---
name: image
description: Get information out of an image — OCR text from a screenshot or scan, or describe what a picture shows. Load when an image is uploaded and you need the text in it or need to understand what it depicts.
---
# Reading images

This skill is for understanding an EXISTING image. If the user wants an
image CREATED (draw, paint, generate a picture), load the `imagegen` skill
instead — it uses the local `image.generate` tool.

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

## Cross-check with cloud vision when the extraction looks partial

Local vision is good at describing a scene but unreliable at *exhaustive*
extraction. Cross-check ONCE with a cloud vision model when ALL of these hold:

- The task demands an exhaustive list or an exact layout — "all the X",
  "every item", a full diagram state (a chess position, a table, a chart's
  data points), or many small text snippets in one image.
- The local answer feels thin for what's visibly in the picture (few items
  from a dense image, missing sections, or you're genuinely uncertain).

    llm.call(task="List EVERY <thing> visible in this image, one per line, none omitted.",
             images=["<path-to-image>"], model="kimi-k3")

(`kimi-k3` has native vision; `gemini-pro` is the alternate.) Merge the two
reads: union of items, conflicts stated. The privacy gate may block the
cloud call (private conversation, unattended run) — then proceed local-only
and say the cross-check was unavailable. Never silently treat a partial
local extraction as complete, and don't loop: one cloud pass, then answer.

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
