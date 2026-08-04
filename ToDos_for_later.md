# To-dos for later

Parked ideas from the Matt Pocock skills evaluation (2026-07). Done and live:
grilling, tdd, diagnosing-bugs, writing-great-skills + /wgs, diff-review.

## Shared-language convention (CONTEXT.md + ADRs)

Port `codebase-design` + `domain-modeling` (+ optionally `grill-with-docs`):
a root `CONTEXT.md` glossary of project domain terms (brain, specialist, preset,
boot posture, confinement, work_root, …) and `docs/adr/NNNN-*.md` decision
records, maintained by the agent as it works.

- Why parked: injecting a glossary into every run costs context proportional
  to its size; it only earns that back once it's large and kept current.
- Cheap 80% variant when picked up: write `CONTEXT.md` for orch-dev once,
  have the agent read it *on demand* (not injected), skip the two skills.
  The skills add the maintenance discipline — adopt only if the glossary
  actually drifts.
- Source: /srv/tmp/skills/skills/engineering/{codebase-design,domain-modeling,
  grill-with-docs} (adapt: CONTEXT.md reading → on-demand; ADR offers stay).

## Browser voice I/O (STT/TTS) — tried and removed

Browser mic dictation (whisper.cpp) + spoken replies (piper) were built
(2026-07: endpoints /api/stt + /api/tts, mic/speak UI, admin Voice pane,
then STT/TTS as slotted presets with a managed whisper process) and then
reverted — voice was not needed and too complicated to include cleanly.
The text-in/text-out `/api/voice` channel for native clients (Android)
predates all that and is unaffected. If voice ever comes back, the three
revert commits (fde4d3e, 8264b26, 14c20dc) point at the full implementation,
and Orpheus-3B (GGUF via llama.cpp + SNAC decoder) remains the
high-quality TTS option over piper.

## Android app (chat client with voice input)

Parked until after JayNet 0.1. Full handoff with verified server contract:
`/srv/android-dev/jaynet-chat-android-handoff.md`.

- Server side is **done**: `/api/voice` accepts `voice:false` (chat mode —
  full markdown, thinking on, normal budgets; safe unattended toolset for
  both modes). Per-user `jn_…` Bearer tokens, server-managed conversations,
  SSE token streaming, cancel — all live and tested.
- Recommended v1: **WebView wrapper** around ask.jaynet.ch (web UI already
  has a narrow layout; every UI change ships to the phone automatically)
  + a `@JavascriptInterface` dictation bridge (SpeechRecognizer/Whisper —
  Web Speech API doesn't work in WebViews). Native Compose app only if
  voice-first (always-listening, barge-in) ever becomes the goal.
