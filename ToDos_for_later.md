# To-dos for later

## Roadmap (2026-08-13, merged review + audit list, ranked)

1. ~~**Context economy / loop discipline**~~ — **done 2026-08**: near-dup
   loop guard (Jaccard on arg tokens for query tools; third reworded repeat
   blocked → synthesize). Tool-result trimming already shipped earlier
   (compaction: stub old large results, image elision, context.pin).
2. **CI workflow + ruff baseline** — no `.github/` exists; suite is CI-ready
   (temp data dirs, no GPU/network, ~66 s). Unblocks ruff (audit sug. 10).
3. ~~**Scheduled, version-tagged eval runs**~~ — **done 2026-08**: Admin →
   Eval → Scheduled runs (interval suites, stale-selector auto-disable,
   skip-while-busy); results record version + brain.
4. ~~**API-key support for adopted endpoints**~~ — **done 2026-08**: preset
   field `api_key_env` (env var NAME; key stays in the env file), honored by
   probes and the litellm render (`os.environ/…` indirection).
5. **Push hygiene + GitHub Releases** — GitHub verified in sync 2026-08-13
   (master + all v0.9.x tags → v0.9.5 027313a). Open: LAN origin check (SSH
   from dev shell has no agent) and creating GitHub Releases — notes for
   v0.9.5 drafted at /tmp/release-notes-v0.9.5.md (not committed; paste via
   the GitHub web UI or `gh release create`).
6. **Backlog + nit cleanup** — this file's stale entries; audit 2026-08-13
   B5 (v0.9.4 tag sits past its bump commit — wontfix, pushed) and B7
   (conftest temp-dir leak — fixed).
7. **Benchmark-informed routing** — feed benchmark results into specialist
   selection. Backlog: modest payoff with a fixed brain/specialist pair.
8. **Android WebView client** — post-1.0 (section below).

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
Docs: docs/admin.md#eval. Cancel endpoint + benchmark-aware statistics:
done 2026-08 (b604eb9).

## Shared-language convention (CONTEXT.md + ADRs)

CONTEXT.md: done 2026-08 (ab8b9bb) — root glossary of domain terms, written
once, read on demand, not injected. Still parked: `docs/adr/NNNN-*.md`
decision records and the `codebase-design` / `domain-modeling` /
`grill-with-docs` skills that add the maintenance discipline — adopt only if
the glossary actually drifts. Source: the Matt Pocock "get things done"
skills collection.

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

Done and live (2026-08): Admin → Presets → "Download from HuggingFace" —
repo file picker with sizes, threaded downloads with progress/cancel, then
"create preset" prefills the editor (name, alias, free port, .conf with
MODEL_PATH, VRAM estimate). Shared core `runtime/hf_pull.py`; the
`scripts/pull-model` CLI wraps it too.

## Managed vLLM (Layer 2 of the backend work)

Layer 1 shipped (2026-08): remote presets accept full endpoint URLs, carry a
`backend` label (llama/vllm/ollama/openai) and `caps` overrides
(vision/thinking); probing matches served_id across multi-model servers;
the thinking switch and vision gating follow backend+caps. External
embed/rerank endpoints documented (docs/models.md).

What Layer 2 would add — JayNet *launching* vLLM itself, not just adopting
a running one:

- Binary registry grows a `type` + command template (`vllm serve {model}
  --port {port} …`) next to the current `{path, device_env}` llama builds;
  a thin vllm launcher beside scripts/start-model.sh translating the .conf
  subset (CTX_SIZE→--max-model-len, …) or honoring EXTRA_ARGS.
- Pluggable metrics: map vLLM's `vllm:*` Prometheus names into the internal
  stats shape `_parse_llama_metrics` fills (web/server.py).
- nvidia-smi path for GPU headroom checks (tools/gpu/status.py is
  rocm-smi-only) — matters the moment vLLM-on-CUDA hosts appear.
- HF downloader accepts safetensors repos for vLLM presets (runtime/hf_pull.py
  is GGUF-only; note 10–100× larger downloads).
- Concurrency: local_concurrency values mirror llama `-np` slots; vLLM
  batches continuously and would want higher caps per backend type.

(API-key support for adopted endpoints shipped 2026-08 — preset field
`api_key_env` naming an env-file variable; probes + litellm render honor it.)

Deliberately stays llama-only: `/v1/rerank` (use TEI/Infinity/Cohere via
`tools.rag.rerank_url` instead), MTP acceptance parsing, GGUF tooling.

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

Done 2026-08 (6ea9b7c) — startup/shutdown hooks moved to the lifespan-context
pattern in web/server.py. Remaining from that note: enable ruff once a
GitHub Actions pipeline exists (roadmap #2 above).
