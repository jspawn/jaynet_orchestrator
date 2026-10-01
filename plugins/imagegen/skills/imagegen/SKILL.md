---
name: imagegen
description: Create images locally from a text prompt (draw, paint, generate a picture/logo/poster/wallpaper/artwork) via the image.generate tool — Qwen-Image on this machine, no cloud. Load when the user wants an image MADE. For understanding an existing image (OCR, describe), load the `image` skill instead.
---

# Generating images (imagegen plugin)

The tool is `image.generate`. It is NOT in the default toolset — if it is
missing from your tools, call `tools.load` with `name="image"` first, then
`image.generate`.

## Workflow

1. Write a specific prompt: subject, style, composition, lighting, and any
   text to render in quotes (Qwen-Image renders quoted text well).
   `negative_prompt` for what to avoid, `size` (default 1024x1024; portrait
   928x1664, landscape 1664x928), `seed` to reproduce.
2. Call `image.generate` ONCE, then wait — a generation takes about a
   minute. Do not poll or re-call while it runs.
3. Deliver: the tool hands the PNG to the user as a download AND copies it
   into your workspace. Use the returned `path` (the workspace copy) for any
   follow-up — vision check, edits. Do NOT call `deliver.files` on it; the
   download is already offered. If your brain is vision-capable, look at the
   result (it is attached, or via the `image` skill) and regenerate with an
   adjusted prompt if it missed the brief.

## What to know

- Generation hibernates the specialist model for the duration (it restarts
  automatically after an idle keep-warm window). Never generate while a
  delegation needs the specialist — finish or wait out the delegation first.
- Everything runs locally; nothing leaves the box.
