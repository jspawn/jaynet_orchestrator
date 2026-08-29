# To-dos for later

Swept 2026-08-16. Shipped items were removed — see `docs/releases/`,
`docs/admin.md` and the git log for what landed (eval harness, harness todo
list, HF downloader, structured preset editor, styled dialogs, FastAPI
lifespan, CI + ruff, scheduled evals, benchmark compare, api_key_env,
loop guard, …).

## Open

### Plugin follow-ups (post-1.1.0)

The plugin system + graphify plugin shipped in 1.1.0 (docs/plugins.md).
Deliberately deferred:

- ~~Plugin downloader/marketplace UI~~ — done post-1.2.0: `.jayplugin`
  export/import in the Plugins tab, plugin admin UIs served from `ui/`,
  `requires_bins` + README discovery. A shared catalog/registry of packs
  stays open.
- ~~Hot-reload on toggle~~ — done post-1.2.0: enable/disable applies live
  (tools/hooks/skills/routes/UI), fresh packs get a "load now" button; only
  new pip dependencies still need a restart.
- ~~Auto-rebuild of the project graph on file change~~ — done post-1.2.0
  (opt-in `plugins.graphify.auto_rebuild` + delay): debounced rebuild after
  a quiet window, only for projects that already have a graph. The staleness
  blind spot is closed too — both web-API edits and agent `fs.*` writes fire
  `on_project_file_changed`.
- Cross-project questions via `graphify merge-graphs`.
- Project graph included in jaypack export/import.
- ~~Wiki pages as graph nodes~~ — done post-1.2.0: deterministic extractor
  (one node per project-wiki page + `references` link edges, default on via
  `plugins.graphify.wiki_nodes`). Saved-chat decisions as graph nodes stays
  open (JayNet-specific extractor graphify upstream doesn't have).
- ~~Bridge the knowledge surfaces~~ — done post-1.2.0: `graph.seed_kg`
  (project graph → curated kg, namespaced + provenance + confirmation) and
  the `rag_excerpt` hook (project-bound `rag.search` surfaces the graph
  neighborhood of its hits). Remaining: wiki pages as graph nodes.
- A second plugin written against the public interface — graphify was built
  by the same hands as the host; a plugin the core authors didn't write is
  the real API test. Candidate TBD (voice or image below would qualify).

### Voice (STT + TTS) as a plugin

Voice was built into core once and reverted as too complicated to include
cleanly (see *Parked* below) — a plugin is precisely the answer to that:
opt-in, disabled by default, no core surface when absent.

- **STT:** whisper.cpp (GGUF) as a managed process or slotted preset, behind
  a `voice.transcribe` tool + `/api/stt` endpoint; browser mic button /
  Android dictation bridge talk to it.
- **TTS:** piper for the cheap path, Orpheus-3B (GGUF via llama.cpp + SNAC
  decoder) as the high-quality option; `voice.speak` tool + `/api/tts`
  endpoint, speak toggle in chat.
- Reuses what the binary registry + preset slots already know (managed
  processes, GPU/CPU placement) instead of re-inventing serve logic.
- The revert commits are in the pre-squash history (search the log for
  "voice") — mine them for the endpoint/UI shapes, keep the plugin boundary.

### Image generation as a plugin

Local-first image generation (Stable Diffusion / Flux via a managed server
or an OpenAI-compatible image endpoint), cloud (OpenAI/Gemini image APIs)
behind the existing taint gate like every other cloud call.

- `image.generate` tool (prompt, size, count → files under outputs/, rendered
  inline in chat like other artifacts) + an admin pane for the backend config.
- Same managed-process pattern as voice: adopt a running server
  (e.g. stable-diffusion.cpp, A1111/ComfyUI API) as a remote preset, JayNet
  launching one itself is the optional Layer 2.
- Privacy: prompts count as conversation content — tainted sessions stay
  local-only, same rule as cloud LLM calls.

### RLM pattern (Recursive Language Models) — native, NOT a plugin

Source: [arxiv.org/abs/2512.24601](https://arxiv.org/abs/2512.24601) +
github.com/alexzhang13/rlm (MIT OASYS lab, pip `rlms`). The core trick is
*context-as-variable*: the long prompt never enters the context window —
it sits in a code environment as an addressable object; the model slices
it programmatically and maps sub-LLM calls over chunks. Beats compaction
(~26% median on GPT-5) because compaction summarizes away what RLM
addresses.

**Decision (2026-08-22): implement the pattern directly, do NOT wrap the
`rlms` package.** RLM is a harness pattern, not an engine (unlike
graphify). Wrapping it would run a second, unmediated agent loop inside
ours: its sub-calls bypass budget accounting, taint gates, and trace.db;
its default `local` REPL is in-process `exec` (own README: not for
production) — a posture regression vs our confined `code.execute`; its
sandbox/client layers duplicate what we own.

**Shipped (2026-08-22, same day):** the mediated `llm_query` /
`llm_query_batched` subcall primitive (per-run unix-socket server,
per-execution grants, budget-billed, taint-gated local-only, traced —
runtime/subcall.py + the ctx.subcall_grant seam), the `context.stage`
tool, the RLM route as option 1 in the `long-document` skill, eval
fixture `seed_code` + the OOLONG-style `rlm-log-aggregate` case.

Remaining follow-ups:

- **Benchmark-tab A/B:** same brain ± `long-document` skill, same seed, on
  `rlm-log-aggregate` — quantify what the doctrine buys each brain.
- **RLM-Qwen3-8B preset test:** the paper's post-trained model (HF) as a
  preset vs our stock brains on those cases — untrained local models write
  measurably worse decomposition code (paper: +28% post-trained over stock
  Qwen3-8B).
- **Plugin route stays possible** only if exact paper behaviors become
  must-haves (persistent versioned REPL, in-REPL compaction): wrap `rlms`
  like we wrapped `graphifyy`.

### Finetuning the brain for JayNet (LoRA, eval-harness-measured)

Data inventory (2026-08-29, live): trace.db has 1,191 runs / 934 with
tool calls / 13.5k tool calls; eval.db has 844 graded trajectories
(468 pass / 376 fail) — a ready-made quality filter — and 87 cases with
both a pass and a fail (DPO pair seeds). Enough for a **targeted** LoRA,
not a broad one; data compounds ~100 graded trajectories per eval suite.

Caveats that shape the pipeline:

- **Self-distillation limit:** the persistent failures (code.delegate,
  council.vote, run.badge) are underrepresented in gold data BECAUSE the
  brain can't produce them. Highest-signal data = teacher-revised
  trajectories: failed eval transcript + judge note → strong cloud model
  rewrites the assistant turns → SFT example.
- **Contamination:** never train on eval-case content (tb/gaia/core by
  test_id) or the benchmark tab becomes a memorization test.
- **Format fidelity:** export must render in exactly the chat template
  llama.cpp serves (tool-call format included), else format drift.
- **Privacy:** no private-tainted runs in the export.

Pipeline to build:

1. `scripts/finetune_export.py`: trace.db + eval.db → JSONL in
   chat-template format (filters: status ok, eval-passed or unflagged,
   no tainted runs, no eval-case content, dedupe).
2. Three datasets: (a) SFT gold trajectories (~600–900 now); (b) DPO
   pairs from the 87 pass/fail splits; (c) teacher-revised failures,
   targeted at the persistent behavior classes.
3. LoRA on the brain, served as a preset via llama.cpp `--lora`.
4. Measurement is free: benchmark tab A/B `brain-base` vs
   `brain-lora-v1`, same seeds — the eval harness IS the finetune loop.
   Promote the adapter only if the persistent failure classes move.

### GitHub Releases

Repo + tags are pushed and CI is green, but no Releases exist on GitHub
yet. Create them from the existing v0.9.x tags; notes can be derived from
`docs/releases/v<version>.md` (paste via the GitHub web UI or
`gh release create`).

### Benchmark adoption — follow-ups (benchlab plugin shipped)

v1 shipped: the `benchlab` plugin imports Terminal-Bench and GAIA Level-1
into the eval harness (docs/plugins.md) — TB lite (container-free subset)
and TB **full** (rootless podman: per-task images, in-container execution +
grading, near-official protocol). What a v2 could add:

- **BFCL** (Berkeley Function Calling) — tool-call AST checks; measures the
  model's tool-calling more than the harness, but a good regression net for
  tool-schema/description changes.
- **SWE-bench Verified subset** — needs per-repo environments; only worth it
  with an optional podman runner (Layer 2; core stays container-free).
- **τ-bench** — needs a user-simulator model; adaptive driver is close but
  not the same protocol.
- **Agent-phase network for full TB** — containers run `--network none`;
  the few tasks needing runtime network fail today. A per-case
  `container.network: true` escape (opt-in, documented) would close the
  last protocol gap.
- **Cross-harness numbers page** — once bench cases accumulate runs, a
  docs page tracking JayNet-condition scores per brain/version.

### Merge `code.execute` + `code.run` (v2 candidate — check evals first)

The two execution tools differ by *intent* (Python snippet vs project
shell), not capability — `code.execute` can `import subprocess` and cover
`code.run`'s small end, and it already grew a bash mode for container
benchmarks. Small brains blur intent-only boundaries (recurring eval
failure pattern, 2026-08). A v2 could merge them into one tool
(`code.run(language: python|bash)`) or at least rename to self-describing
`code.python` / `code.shell`.

**Condition: evidence first.** The 2026-08-29 cluster was ~70% a config
artifact (code.run confirmation-gated on live via `sandbox_prefix: []`
while its description promised ungated) — fixed by enabling the devbox
container (container = sandbox → ungated) plus sharpened descriptions on
both tools (no subprocess dodge; a decline is final). Only merge/rename if
the **next eval suite with devbox live** still shows systematic
tool-choice confusion in `code.*`/`skill-load`/`j-space-loop` cases.
Costs to weigh then: the merged description must cover python machinery
(EXEC_OUT/EXEC_WORK, llm_query helpers) + shell + devbox + container
branches (long descriptions lose small brains too); blast radius spans
gate prompt, coding skills, eval `must_use_tools` asserts, catalog,
benchlab.

### Managed vLLM (Layer 2 of the backend work)

Layer 1 shipped: remote presets adopt an already-running llama-server /
vLLM / Ollama / OpenAI-compatible endpoint (full URLs, `backend` label,
`caps` overrides, `api_key_env`, served_id probing). What Layer 2 would
add — JayNet *launching* vLLM itself:

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

Deliberately stays llama-only: `/v1/rerank` (use TEI/Infinity/Cohere via
`tools.rag.rerank_url` instead), MTP acceptance parsing, GGUF tooling.

### Android app (chat client with voice input)

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

### JayNet as a pip package (`pip install jaynet-orchestrator`)

Feasible and worth doing once the plugin system exists. The work:

- `pyproject.toml` with console entry point (`jaynet setup`, `jaynet serve`
  wrapping scripts/setup.sh + web/server.py uvicorn launch).
- Ship `web/static/`, `config/` templates, `skills/`, `prompts/`, `presets/`
  as package data; resolve them via `importlib.resources` instead of
  repo-relative paths (most paths already flow through `runtime/paths.py`
  + env overrides — audit the remaining repo-relative reads).
- Default dirs stay `~/jaynet-data` / `~/jaynet-models`; first run of
  `jaynet setup` writes the env file like scripts/setup.sh does today.
- Plugins become real pip packages too (`pip install jaynet-graphify`),
  discovered via the plugin manifest entry point instead of a directory scan.
- Open: systemd units and nginx examples stay docs-level (not pip business);
  llama.cpp binaries remain out of scope (user-built or quickstart-fetched).

## Parked (revisit only if …)

### ADRs + design-discipline skills

`docs/adr/NNNN-*.md` decision records and the `codebase-design` /
`domain-modeling` / `grill-with-docs` skills from the Matt Pocock
collection — adopt only if CONTEXT.md (the root glossary) actually drifts.

### Browser voice I/O (STT/TTS) — tried and removed

Browser mic dictation (whisper.cpp) + spoken replies (piper) were built
(2026-07: endpoints /api/stt + /api/tts, mic/speak UI, admin Voice pane,
then STT/TTS as slotted presets with a managed whisper process) and then
reverted — voice was not needed and too complicated to include cleanly.
The text-in/text-out `/api/voice` channel for native clients (Android)
predates all that and is unaffected. If voice ever comes back, the revert
commits live in the pre-squash history (search the log for "voice"), and
Orpheus-3B (GGUF via llama.cpp + SNAC decoder) remains the
high-quality TTS option over piper.
