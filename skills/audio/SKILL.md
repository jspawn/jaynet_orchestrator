---
name: audio
description: Transcribe audio files (voice notes, recordings, podcasts) to text with the local whisper server. Load when the user uploads or references an audio file and wants a transcript or its content.
---
# Transcribing audio

Use **audio.transcribe** to turn an audio file into text:

    audio.transcribe(path="<path-to-audio>")                # auto-detect language
    audio.transcribe(path="<path-to-audio>", language="de") # hint the language

- `path` must be inside your workspace (same confinement as the fs.* tools).
  wav/mp3/ogg/flac/m4a and anything else ffmpeg-backed whisper.cpp reads.
- The tool posts to the local **stt slot** — a whisper.cpp whisper-server,
  CPU-only, assigned in Admin → Presets (Boot model slots). While the slot is
  empty the server is down and the call returns a clear error saying so;
  report that to the user instead of retrying or guessing the content.
- Transcripts are **private**: they stay on the box and never flow to a cloud
  model unless the user enables sharing for the run.

## Notes

- Long recordings take a while on CPU — that's normal; the tool waits for the
  server.
- Never invent a transcript. If the tool returns empty text (silence or
  non-speech audio), say so.
