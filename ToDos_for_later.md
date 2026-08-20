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

- Plugin downloader/marketplace UI (v1 install = copy dir into DATA/plugins).
- Hot-reload on toggle (today: restart required).
- Auto-rebuild of the project graph on file change (today: dirty flag + hint).
  Includes closing the staleness blind spot: `on_project_file_changed` only
  fires on web-API edits — agent writes via `fs.*` into the project's
  work_root never mark the graph dirty. Fix by firing from the fs tool path
  or mtime-checking in `read_status`.
- Cross-project questions via `graphify merge-graphs`.
- Project graph included in jaypack export/import.
- Wiki pages + saved-chat decisions as graph nodes (JayNet-specific
  extractors graphify upstream doesn't have).
- Eval cases for "agent uses graph.query before grepping".

### GitHub Releases

Repo + tags are pushed and CI is green, but no Releases exist on GitHub
yet. Create them from the existing v0.9.x tags; notes can be derived from
`docs/releases/v<version>.md` (paste via the GitHub web UI or
`gh release create`).

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
