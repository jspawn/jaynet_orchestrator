---
name: omnivoice
description: Turn text into speech locally (read aloud, generate a voice message/podcast snippet/audio greeting) via the audio.speak tool — OmniVoice on this machine, 600+ languages, voice design and voice cloning, no cloud. Load when the user wants audio MADE from text. For transcribing existing audio, load the `audio` skill instead.
---

# Text-to-speech (omnivoice plugin)

The tools are `audio.speak` and `audio.clone`. They are NOT in the default
toolset — if missing from your tools, call `tools.load` with `name="audio"`
first.

## Workflow

1. Call `audio.speak` with the text. Shape the voice either by name
   (`voice=` a registered clone) or by description (`instructions=`,
   e.g. "warm elderly female, calm, slow" — voice design). `language=`
   when not the configured default. Non-verbal symbols like `[laughter]`
   work inline.
2. Deliver: the tool hands the WAV to the user as a download AND copies it
   into your workspace. Use the returned `path` for any follow-up. Do NOT
   call `deliver.files` — the download is already offered.

## Voice cloning

`audio.clone` action=register with `name`, `ref_audio` (a WAV in the
workspace — a few seconds of clear speech) and `ref_text` (the EXACT
transcript; it anchors the clone). Then `audio.speak voice=<name>`.
Cloned voices live in server RAM — re-register after a backend restart.
Only clone a voice the user has the right to clone.

## What to know

- The first call pays the model load (~half a minute); the backend stays
  warm for `keep_warm_s` (default 600) and then shuts down. A burst of
  speech requests pays the load once.
- Everything runs locally; nothing leaves the box.
