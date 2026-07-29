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

## Orpheus-3B as high-quality TTS option

Piper covers voice output for now (instant, CPU). Orpheus-3B-0.1-ft (GGUF)
would give expressive voices + emotion tags when quality matters more than
speed.

- Runs on the existing llama.cpp toolchain (ROCm or CPU): GGUF generates SNAC
  tokens, a separate SNAC decoder (ONNX/PyTorch) turns them into 24 kHz audio.
- CPU speed ≈ 0.3–0.5× realtime (~83–91 tok/s needed for 1×) — fine for our
  batch TTS path (short voice-persona replies), or partial GPU offload in a
  free slot.
- Wiring options: (a) run an Orpheus HTTP server as a managed process and set
  `voice.tts.command` to a small curl wrapper (no code change), or (b) add a
  `voice.tts.url` mode to `web/routes_voice.py` for OpenAI-compatible
  /v1/audio/speech servers (e.g. Lex-au/Orpheus-FastAPI).
- Parked 2026-07-29: piper is good enough; revisit if voice quality matters.
