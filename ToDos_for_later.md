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
- Bridge the knowledge surfaces: seed `kg.*` entities/relations from
  graphify graph nodes (auto-derived → curated), and let `rag.search`
  surface graph excerpts. Today memory/kg/rag/wiki/graph coexist but never
  read from each other.
- A second plugin written against the public interface — graphify was built
  by the same hands as the host; a plugin the core authors didn't write is
  the real API test. Candidate TBD.

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
sandbox/client layers duplicate what we own. JayNet already has ~80%:
workspace files ARE context-as-variable, `code.execute` runs over them
context-free, `agent.spawn` is recursion with budgets. The one missing
primitive is a *mediated* sub-LLM call from inside code execution.

The build, in dependency order:

1. **In-code `llm_query` / `llm_query_batched`** — the missing primitive.
   `code.execute` gets a per-run helper: env-injected loopback endpoint +
   per-run token (e.g. `/api/internal/subcall`, owner/run-scoped,
   short-lived). Sub-calls route through the RUN's own model client so
   they count against the run budget, inherit private-taint (local-only
   alias when tainted — never the cloud gate), and land in trace.db.
   Hard caps FIRST: max sub-calls per execution, concurrency bound,
   timeout; model-written code multiplying LLM calls is the main risk.
2. **`context.stage` tool** — dumps oversized text/tool output into a
   work_root file, returns path + size + a one-line "address it, don't
   read it" nudge. Small; the bias against hauling bulk into context.
3. **Skill `rlm` (or `long-context`)** — the doctrine: don't read the big
   thing; slice programmatically, map `llm_query` over chunks, reduce,
   verify. Existing `long-document` skill likely merges into this.
4. **Eval cases** — OOLONG-style long-context QA: answer needs
   aggregation across a large fixture (e.g. generated 200KB log — fixture
   generation at seed time, not 200KB literals in YAML). Then
   benchmark-tab A/B: same brain ± skill, same seed.
5. **Follow-up test:** the paper's post-trained RLM-Qwen3-8B (HF) as a
   preset vs our stock brains on those cases — untrained local models
   write measurably worse decomposition code (paper: +28% post-trained
   over stock Qwen3-8B).

Open only if the exact paper behaviors become must-haves (persistent
versioned REPL, in-REPL compaction): the plugin route stays possible
later, wrapping `rlms` like we wrapped `graphifyy`.

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
