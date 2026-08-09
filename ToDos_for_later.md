# To-dos for later

Parked ideas from the Matt Pocock skills evaluation (2026-07). Done and live:
grilling, tdd, diagnosing-bugs, writing-great-skills + /wgs, diff-review.

Done and live (2026-08): the **harness todo list** — `todos` tool + per-run
state, live ToDos side panel (collapsible, per-item expander, mobile-collapsed),
compaction-proof re-injection, architect UNITS → list, executor-child sync.

Done and live (2026-08): the **eval harness** — cases in `evals/` + custom
layer, scripted/adaptive runner through the real loop, cloud judge with local
fallback, eval.db trends, gated proposals inbox, flags→test generation,
`.jaypack` sharing, `eval.run/list/report` tools, Admin → Eval tab incl.
Benchmark shootouts (N variants × reps, per-brain compare matrix).
Docs: docs/admin.md#eval. Open eval items: no cancel endpoint for a running
suite/benchmark (a stop flag the job loop checks between cases); Statistics
is not benchmark-aware (no per-label filter — reps move trend/flakiness).

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
- Source: the Matt Pocock "get things done" skills collection
  (`engineering/{codebase-design,domain-modeling,grill-with-docs}` — adapt:
  CONTEXT.md reading → on-demand; ADR offers stay).

## Browser voice I/O (STT/TTS) — tried and removed

Browser mic dictation (whisper.cpp) + spoken replies (piper) were built
(2026-07: endpoints /api/stt + /api/tts, mic/speak UI, admin Voice pane,
then STT/TTS as slotted presets with a managed whisper process) and then
reverted — voice was not needed and too complicated to include cleanly.
The text-in/text-out `/api/voice` channel for native clients (Android)
predates all that and is unaffected. If voice ever comes back, the revert
commits live in the pre-squash history (search the log for "voice"), and
Orpheus-3B (GGUF via llama.cpp + SNAC decoder) remains the
high-quality TTS option over piper.

## HuggingFace downloader in the admin GUI

Download GGUFs from HuggingFace into the models dir from the admin panel
(Presets area) instead of the shell — repo/file picker, progress, then
"create preset from this file". `scripts/pull-model` already does the CLI
side; the GUI version could wrap it or use `huggingface_hub` directly.

## Android app (chat client with voice input)

Parked until after JayNet 1.0. Full handoff with verified server contract
lives outside the repo (author's notes: jaynet-chat-android-handoff.md).

- Server side is **done**: `/api/voice` accepts `voice:false` (chat mode —
  full markdown, thinking on, normal budgets; safe unattended toolset for
  both modes). Per-user `jn_…` Bearer tokens, server-managed conversations,
  SSE token streaming, cancel — all live and tested.
- Recommended v1: **WebView wrapper** around the hosted web UI (it already
  has a narrow layout; every UI change ships to the phone automatically)
  + a `@JavascriptInterface` dictation bridge (SpeechRecognizer/Whisper —
  Web Speech API doesn't work in WebViews). Native Compose app only if
  voice-first (always-listening, barge-in) ever becomes the goal.

## Styled dialogs to replace native prompt()/confirm()/alert()

Done and live (2026-08, C4): `web/static/dialog.js` — self-contained
promise-based `dlgAlert`/`dlgConfirm`/`dlgPrompt` (own CSS/DOM, themed via
CSS vars, Esc/Enter, click-outside), all ~40 call sites migrated
(app.js, files.js, admin.html), the old `#modal` markup retired.

## FastAPI @app.on_event → lifespan migration
The suite emits ~1000 deprecation warnings, dominated by FastAPI's
`@app.on_event("startup"/"shutdown")` (web/routes_procs.py and friends).
Migrate to the lifespan-context pattern when touching that code anyway —
pure tech debt, no functional issue. Also the natural moment to enable
ruff in CI (audit suggestion 10) once a GitHub Actions pipeline exists.
