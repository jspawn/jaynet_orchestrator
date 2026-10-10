# To-dos for later

Swept 2026-08-16, re-swept 2026-09-30 (image-gen plugin, prompt pass
batches 1+2 + the diet half of 3, STT back in core, CLM as the Jev
successor, cross-harness numbers page = brain-bakeoff.md — all struck
below). Shipped items were removed — see `docs/releases/`,
`docs/admin.md` and the git log for what landed (eval harness, harness todo
list, HF downloader, structured preset editor, styled dialogs, FastAPI
lifespan, CI + ruff, scheduled evals, benchmark compare, api_key_env,
loop guard, …).

## Open

### Persistent semantic repo map (idea, from J-Space SV1)

SV1's one genuinely new idea for us: a per-project `repo-map.json`
(summary/areas/facts-with-evidence/dependencies/tests/unknowns) that is
**freshness-checked** — content hashes detect when the map went stale
against the tree, and gates require a current map before delivery. Our
REPO MAP orientation spawn is one-shot; nothing persists or detects
drift. Candidate shape: project-level artifact + a drift check
(possibly a pre-turn or final guard nudge when the map is stale), not a
vendored script. Source: upstream `control.py repo sync/view/check`.

### Doc/quality audit 2026-10-05 (Claude) — tier 1+2 backlog

From `/srv/orch-dev-audits/claude_doc_audit_05102026.md` (ratings: code
quality 7.5, ease of understanding 5.5, docs 8.5). S-batch (#2, #4,
#12, #14, #15) shipped in v1.20.3 (2026-10-06); #9's stray Unreleased
block already fixed. Remaining:

- **#1 [L] `run()` gate extraction** — phase 1+2 shipped (2026-10-09):
  the ~16 pre-execution tool-call checks are now the registered
  `DISPATCH_GATES` pipeline (`runtime/dispatch_guards.py`, ablation
  name-keyed per gate), the setup closures (`_auto_delegate`,
  `_wrap_up_or_salvage`, `_stuck_hit`, `_todos_update`,
  `_pick_delegate_route`) are `DispatchGateContext` methods, ctx.spawn is
  `SpawnService` (`runtime/spawn_service.py`, with `_child_budget` and the
  nested-provider helpers), post-exec recording is
  `_record_tool_results`, and the final-answer chain is applied by
  `final_guards.apply_final_guards`. Follow-up (same day): the config-parsing
  setup moved to `runtime/run_setup.py` (`RunSettings` +
  `parse_run_settings`, rationale comments on the fields) and the redundant
  `rs.*` re-inits collapsed onto the `RunState` dataclass defaults.
  Then (same day): message assembly → `_assemble_messages`, routing
  nudge/procedure autoload/adaptive thinking → `_apply_brain_nudges`,
  scratch-dir setup → `_setup_scratch`, delegate/stuck/strength arming →
  `_arm_delegate_gates`, verify pre-run → `_verify_baseline`, requirements
  seeding → `_seed_requirements`, registry construction → `_build_guards`,
  anchor/state-file/bounce parsing → `RunSettings` fields. Final slice:
  the turn-loop's model-turn block → `_run_model_turn` (prefill signal,
  call, think-strip, telemetry, usage, malformed-assistant guard), the
  per-turn anchor assembly → `_turn_anchor`, compaction invocation →
  `_maybe_compact`, and the finish/result tail → `_finish_run`. **Target
  reached: `run()` 2,594 → 767 lines, complexity 257 → ~80.** What stays:
  the turn orchestration itself (guard iteration, dispatch, stall
  bookkeeping), the except handlers, and the small ctx-seam closures
  (`_expand_tools`, `_subcall_grant`, `_ask_user`, …) — compact,
  single-use, and the loop's actual job. Safety net held:
  test_loop_regressions + full suite green at every step, event names
  byte-identical.
- **#3 [M, incremental] decision log** — `docs/decisions/` (one short
  file per decision: context, decision, live/eval evidence, date); code
  keeps one `# why: D-NNNN — …` line. Start with loop.py's 57 "live:" /
  "audit #…" references; move the rest whenever touching a file.
- **#5 [M] config single source of truth** — generate config-help.yaml
  from the pydantic Field descriptions; extend the typed schema past
  agent/budgets/tool_selection/eval (105 of 448 leaf keys) into
  tools/models/processes; delete the ~58 hand coercions as sections get
  typed.
- **#6 [M] hotspot splits** — SpecialistDelegate.execute (398 lines,
  complexity 115) → route/prepare/run/verify/review/envelope;
  eval_runner.run_case (328, 107); eval_cases.validate_case_dict (81) →
  pydantic model; BrowserWebsec._argv (80) → table-driven.
- **#7 [L, opportunistic] web layer** — route modules from register()
  closures to APIRouter + Depends; typed AppState dataclass instead of
  the 43-field SimpleNamespace.
- **#8 [M–L] frontend split** — admin.html (4,493 lines, ~3,281 inline
  JS) → one JS module per admin tab; app.js (2,866) → chat stream /
  rendering / composer / side panels.
- **#9 residual [S] release discipline** — one release per batch (8
  tags on 2026-10-05 was too many); smoke-test the headline feature
  live BEFORE tagging (v1.20.0's broken model.measure is the lesson);
  CHANGELOG to short bullets, narrative lives in release files; archive
  the 61 release-note files into one per minor version.
- **#10–#13 [incremental hygiene]** — 268 `except Exception` (56
  try/except/pass, 44 raise-in-except without `from`; enable ruff
  B904/S110 with baseline); shrink the mypy baseline each release +
  Protocol instead of mixin contracts; ORCH_* → JAYNET_* env migration
  (22 vs 12) with a drop date; glossary terms marked core vs plugin.

### Still-open audit 2026-10-06 (Claude) — what this session did NOT fix

From `/srv/orch-dev-audits/claude_audit_still_open_06102026.md`. Fixed
in this batch: the `all_owners` admin-only escape, `/imp` local swap
admin gate, `doc.pages`/`doc.tree` private, `model.measure` admin-only +
the code-side `admin_only_tools` floor, `is_admin` failing closed, the
bounce-cap well-formed fallback. Still open:

- **Role policy remainder [M]** — a capability tier on `Tool` plus a
  completeness test (any tool that spawns host processes or calls
  ServeStart/ModelUse/JobStart must be admin-tier or sandboxed).
- ~~**#4 authored checks red→green [M]**~~ — shipped 2026-10-10: the
  authored `CHECK:` command re-runs against a detached worktree at the
  pre-spawn git ref (new check files overlaid, modified files keep their
  old content); green on both trees → `verified` flips False. Non-git
  workspaces get a `baseline_note` instead.
- ~~**#6 guard telemetry [M]**~~ — shipped: `scripts/eval-peek.py` closes
  every report with per-guard fire rate + pass-after-fire vs quiet. Still
  open: the first `guards_off` ablation using these numbers (also listed
  under the 2026-09-23 leftovers; benchmark UI lacks a guards_off input).
- **#10 prompt/tool diet [M]** — gate prompt at 1,723 words; no
  dispatch-mode variant; ~4,600 tokens of core tool schemas. Overlaps
  the 16-habits Batch 3 above.
- ~~**#11 h5i lane hardening [M, if enabled]**~~ — shipped 2026-10-10:
  SSRF guard on `browser.browse` open/read and the direct-dial verbs
  (`socket`, `grpc --url`) via the core `ssrf_refusal` helper; per-call
  `allow` may only narrow `plugins.h5i.allow` (widening refused before
  spawn); recon `paths` + websec replay/experiment/matrix/sequence/socket/
  grpc-call now confirmation-gated; `browser.browse` is `private` like the
  sec tools (requests/audit/screenshot expose captured credentials).
- **#14 smaller fixes [S each]** — `fs.edit`: optional `compile()`
  check for `.py` edits; `model.measure`: read the server process's
  `/proc/<pid>/smaps_rollup` instead of the MemAvailable delta
  (undercounts mmap'd weights); `proc.run`: cap stdout/stderr buffering;
  LiteLLM fallbacks optionally off during eval runs; `test.run`: give
  `tools.test.sandbox_prefix` a firejail default (defence in depth).
- ~~**Overlay drift warning (own finding, 2026-10-06)**~~ — shipped:
  `gate_prompt.staleness` + the worker-prompt equivalent flag an overlay
  older than the shipped file (boot log warning + warnbox badge in
  Admin → Harness → Prompts, both gate and worker parts).

### ~~searXNG health alert~~ — shipped 2026-10-10

The searXNG container sat **exited for ~7 days** and every "web search
degraded" judge note in that delta traced to it — the search fallback
chain absorbed it silently, so nobody noticed. Shipped: `/api/admin/status`
auto-probes configured sidecars (searXNG via `tools.web.search_endpoint`,
a local jev backend) alongside LiteLLM + `web.services`, deduped by URL —
"configured but unreachable" now shows on the Overview card.

### Prompt optimization pass (the 16-habits audit, 2026-09-22)

- ~~**Batch 1 (low risk)**~~ — shipped: output-format/FINAL ANSWER directive
  moved to the recency slot; `web.search`/`web.fetch`/`rag.search`
  descriptions carry explicit fallback contracts ("nothing found → say so,
  never interpolate").
- ~~**Batch 2 (medium)**~~ — shipped as far as sensible: one pure negative
  rewritten positive; the contrast pairs that name the trap stayed by
  design. Untrusted-output XML marking deliberately skipped — tool results
  already ride the structured chat tool role.
- **Batch 3 (structural):** the diet half shipped (admin-facing plugin
  enumeration, changelog-speak, `/charter` + `/llmwiki` mentions cut from
  the gate prompt). Still open: splitting situational directives
  (pinned-sources, high-stakes vote, …) into just-in-time injections that
  fire only when relevant — needs the injection path verified per brain
  chat template (qwen3.5-family raised on mid-history system notes).
- Rule the audit confirmed we already beat: personas, monolithic prompts,
  schema enums, context rot, prompt-security, session clearing — all
  mechanical here. jevify specialist-pitfall section in delegation packs
  is the remaining #14 idea (procedures cover most of it).

### Decision model (Jev-type) for delegation routing (+ compaction)

**v1 shipped (2026-09-22):** `plugins/jev/` — Open-Jev sidecar integration:
`jev.decide` tool (choice/noul/score with calibrated probabilities) and a
`route_request` core hook that classifies each request into a
`models.strengths` tag (threshold-gated, keyword router as fallback). Setup
in the plugin README (server runs separately — torch + pinned Qwen base;
non-autoregressive decision heads don't run as GGUF in llama.cpp).

**Quality check DONE (2026-09-22): the IDEA holds, the open weights don't
(yet).** Open-Jev 2B (local GPU) lost to the keyword router on 20 real eval
prompts (coding → "general" at 0.79; OOD training mixture of synthetic
business decisions). Hosted TypeSafe Jev via the plugin's `openrouter`
backend (`~typesafe/jev-latest`) routed the same prompts nearly perfectly:
coding/research 0.92–1.00, vision 0.99, chat → general 0.96, ~0.4s,
~$0.0003 total — including research/vision/multi-step routes the keyword
router never fires. Decision (user, 2026-09-22): STAY KEYWORD —
cloud-routing every request's text is the wrong default for a local-first
box. Plugin ships disabled with `route: false`. Revisit when an open
checkpoint trained on intent routing lands (Open-Jev 27B run / V3 stage),
or test the 9B (~18 GB VRAM — doesn't fit the current layout).

**jevify option LIVE (2026-09-22):** [jevify](https://github.com/fidecastro/jevify)
sidecar serves the Jev API from the running Qwen3.8-27B specialist
(systemd `jevify.service`, :8600, recipe `/srv/data/jevify/specialist.llamacpp.yaml`;
first live call: coding route at p=0.997, 604 ms). Route bench: jevify over
the specialist hits 63.4% routing accuracy (keywords: 13.2%). jevify can
now register with the process manager (`manage_sidecar` + `recipe`) —
boot start, auto-restart, clean shutdown.

**CLM shipped (1.14.3) as the System-One successor:** the `clm` plugin
(Contrastive-LM-8B, purpose-built contrastive decision model, ~30 ms warm,
SOTA verifier on Terminal-Bench 2.1) — `clm.decide`/`clm.rank`, stdlib-only,
wire-compatible with the Jev contract, local-only. Routing hook ships off
(keywords stay default until the A/B). With Cyber-Tiel's 81% voluntary
delegation the routing problem largely solved itself; the candidate list
below is thereby settled.

Remaining:

- **Compaction keep/drop**: Score/noul questions per segment during
  compact — cheaper and more stable than the brain judging itself. Needs a
  compaction hook point (runtime/compact.py), not built yet. Note: the
  routing OOD result cautions this too — test on real segments first.

~~Other candidates if Open-Jev disappoints: kev-9b (LoRA on Qwen3.5-9B,
systemone contract), APUS-OpenJev (4B/9B), Laya (pip, Apache-2.0,
mmBERT-base, ~33 ms CPU, ECE 0.081).~~ — settled by the CLM plugin.

### vLLM Radiance MXFP4 experiment (the 185 tok/s claim)

The dual-R9700 writeup (alexkmiller.com, 2026-09) got Qwen3.8-27B from
29.7 tok/s (tuned llama.cpp) to **184.9 tok/s** with a patched vLLM
"Radiance" build: MXFP4 weights through RDNA4's native WMMA + speculative
decoding. Our specialist is exactly that model class on exactly that
hardware, and eval wall-clock is specialist-bound.

Try: serve the dense specialist via Radiance, adopt it as a remote preset
(vLLM serving mode exists), benchmark the tb/gaia suite delta. Open
questions: Radiance is a third-party patch build (track upstreaming);
vLLM boots slower and doesn't hot-swap like our llama.cpp slots — the
swap lifecycle (dolphin-for-security etc.) would need a vLLM-aware path
or stay llama.cpp-only for swappable slots. llama.cpp stays the native
runtime either way; this is a specialist-slot experiment.

Mined + applied from the same writeup (shipped, see CHANGELOG):
tensor-split default for multi-GPU presets, MMAP=off load-mode key,
ubatch guidance. FP4 weight quants deliberately skipped: no native FP4
WMMA on gfx1201 (memory savings only, no speed).

### Procedure library (distilled frontier process for small models)

Frontier models beat small models on agentic tasks mostly by *process
discipline*, not knowledge — and procedures are distillable. v0 shipped as a
skill (`skills/implement-from-spec`): the procedure is a SKILL.md, loads via
`skill.load`, and is already jaypack-shareable as kind `skill`. The full
system, in order:

1. ~~**Validate the v0 format**~~ — done post-1.7.1: targeted eval flipped
   tb-chem-property-targeting fail→pass with procedure_autoload firing.
2. **More procedures** — debug/fix (`debug-and-fix`) and research/lookup
   (`research-and-verify`) shipped post-1.7.1 with selector keywords in the
   defaults + shipped config. long-multi-step deliberately skipped: todos +
   j-space already cover it. Remaining value: per-domain procedures mined
   from eval clusters (step 5 feeds this).
3. **Shape tags + selector** — procedures get a `shape:` tag in frontmatter
   (implement-from-spec, debug, research, …). Selection: keyword heuristics
   on the request first, one cheap classifier turn as fallback, then
   auto-`skill.load` at run start (user-visible, overridable). Conservative:
   only auto-load on confident matches.
4. ~~**Loop-enforced checkpoints**~~ — done post-1.7.1: procedures carry a
   `checkpoints:` frontmatter list; the loop appends it to stall-ladder
   rungs and nudges once (`procedure_check`) against it before accepting a
   final answer.
5. ~~**Distillation miner**~~ — shipped: `runtime/procedure_miner.py` +
   endpoints (`POST /api/admin/evals/mine-procedure`, `POST
   /api/admin/flags/{id}/mine-procedure`). The judge contrasts a case's
   PASS/FAIL history (or a flag's scrubbed runs + user note) into ONE
   SKILL.md draft; validated hard (name/shape/checkpoints), returned to a
   review modal, saved via Studio with `draft: true` — a new frontmatter
   flag that keeps drafts invisible to the model (cached discovery +
   catalog + autoload filter them) until a human clears it. Flag privacy
   unchanged: runs are pre-scrubbed by the shared flags assembly.
6. ~~**jaypack kind `procedure`**~~ — shipped as ANNOTATION, not a new
   kind (deliberate: procedures ARE skills, a duplicate kind buys nothing):
   build_pack/inspect_pack carry the `shape` tag in the manifest, the
   import dialog + success note show the procedure trust line ("auto-loads
   on matching requests — review the checkpoints"). Studio lists draft +
   shape badges.
   Remaining value: per-domain procedures mined from eval clusters (step 5
   feeds this — run the miner on cases with pass+fail history).

### ~~Multi-GPU slots + swap-back lifecycle~~ — SHIPPED in 1.9.0

Shipped: the eviction planner (port + every pinned GPU, brain included via
delegate's `include_brain`), per-card VRAM-free waits, swap-back restore
after delegate child runs (`models.swap_back: true`), the preset editor's
standard/all/raw views + per-GPU device checkboxes + live free VRAM, and
nvidia-smi probing. See docs/releases/v1.9.0.md + docs/model-placement.md.
Remaining follow-up ideas: an eval case for the 2-GPU brain ↔ 2-GPU
specialist round trip, and watching real swap timings for whether the
delegate batching guidance needs teeth.

### Plugin follow-ups (post-1.1.0)

The plugin system + graphify plugin shipped in 1.1.0 (docs/plugins.md).
Deliberately deferred:

- ~~Plugin downloader/marketplace UI~~ — done post-1.2.0: `.jayplugin`
  export/import in Admin → Harness → Plugins, plugin admin UIs served from `ui/`,
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

### Voice (STT + TTS)

STT landed back **in core**, not as a plugin (2026-09): whisper.cpp as a
helper-slot preset (WHISPER=on preset mode, CPU/ROCm binaries in the
registry), `runtime.paths.STT_URL` default, and mic dictation in the chat
composer (browser records, resamples to 16 kHz mono WAV client-side). What
remains from this item:

- **TTS:** piper for the cheap path, Orpheus-3B (GGUF via llama.cpp + SNAC
  decoder) as the high-quality option; `voice.speak` tool + `/api/tts`
  endpoint, speak toggle in chat.
- The revert commits are in the pre-squash history (search the log for
  "voice") — mine them for the endpoint/UI shapes if TTS gets picked up.

### Docling plugin (layout-heavy documents)

The light lane shipped instead (`doc.extract`: pypdf + openpyxl + stdlib
docx, `rag.index` auto-converts) because docling in core means torch +
layout models in the main venv — poor trade against the lean-install
posture. Revisit as an optional **plugin** when real scanned or
table/layout-heavy PDFs show up and pypdf's text layer isn't enough:

- `doc.convert` tool backed by docling, deps isolated to the plugin's own
  venv or the devbox container — never the runtime venv.
- Hook the same rag.index auto-convert path (plugin overrides the light
  lane when enabled).
- Until then: scanned PDFs go through the `pdf` skill's OCR venv.

### ~~Image generation as a plugin~~ — SHIPPED in 1.14.3/1.15.0

Local text-to-image (Qwen-Image-2.1 GGUF via stable-diffusion.cpp
sd-server): `image.generate` hibernates the specialist slot, serves the
diffusion backend, stages the PNG, and a keep-warm reaper restores the
slot afterwards; sd-server is registered for shutdown. Discoverable via a
shipped skill + `image` keyword namespace; deliverables render inline in
chat (images/SVG/PDF/HTML). Cloud image APIs behind the taint gate remain
an open add-on if ever wanted.

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
production) — a posture regression vs our confined code sandbox; its
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

Repo + tags are pushed and CI is green. Release notes are produced per tag
(`docs/releases/v<version>.md`; longer-form notes for 1.9.1 and 1.15.0 were
handed over as text/file). What remains is purely the web-UI step: create
the GitHub Releases from the tags, notes pasted from the release files
(`gh release create` works too once `gh` is installed).

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
- ~~**Cross-harness numbers page**~~ — done in spirit:
  `docs/brain-bakeoff.md` tracks per-brain scores across the fixed case
  list (fourteen candidates and counting), with judge-note lessons per
  column. A TB/GAIA-flavored variant of the same table stays open.

### Project execution profiles (opt-in per-project containers)

Lesson from the tb compose work (0ea6005): the hard part — routing
`code.run`/`code.execute` into a container via a `run_overrides`
tools_patch — is already battle-tested (eval-only today; the static
config path stays stripped for chats, audit C1). Projects get the same
capability with an INVERTED lifecycle: eval containers are throwaway
per run; a project container starts on first use, stays warm across
runs, stops on idle/on demand, dies with the project.

- **Opt-in, never default.** Most projects are fine on the shared
  devbox; a container per project by default is image-management
  burden for no gain. Where it earns its keep: dependency conflicts
  (numpy 1.x vs 2.x, .NET 8 vs 10 — the shared box can't serve both),
  multi-service dev stacks, privacy-aligned network policy.
- **The profile**: `execution: {image | packages, network: on|off,
  compose: optional path}` in the project settings, default empty
  (= shared devbox). When set, run start injects the tools_patch from
  the project's profile — same channel eval uses.
- **Compose dev stacks are the big win.** A project that IS a
  multi-service app (web + postgres + redis) gets "agent brings up the
  project's own docker-compose, runs the integration tests against it,
  tears it down" — integration testing the agent today can only
  hand-wave. Reuse `_container_start_compose`/`_compose_stack_down`
  from runtime/eval_runner.py (they're case-scoped; factor the generic
  parts into runtime/ or a tools/code helper).
- **work_root bind-mount** — same sharing semantics as eval: host
  fs.* tools and in-container code see the same files.
- **Network policy meets taint** — a tainted/private project could
  force `--network none` on its container, extending taint from
  "what leaves for the cloud" into execution isolation.
- **Env allowlist + preflight posture** from the eval work: no
  secrets into containers; backend unavailable → fall back to the
  shared devbox with a note, never crash the run.
- **UI**: project settings gain an execution section (image/packages
  picker, network toggle, compose path); the project card shows the
  container state (running/stopped) next to the graph state.
- Deliberately NOT copied from eval: `--rm` throwaway semantics, the
  2g/2cpu eval bounds, per-run teardown. And NOT a per-project
  container for the models — execution only; llama servers stay
  host-side (GPU access, serve lifecycle already manages them).

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

**Started 2026-10-07** — native Kotlin/Compose scaffold lives in
`/srv/android-dev/` (separate repo-to-be; README there covers build, the
server contract, and design decisions). Verified server contract handoff:
author's notes jaynet-chat-android-handoff.md.

- Server side is **done**: `/api/voice` accepts `voice:false` (chat mode —
  full markdown, thinking on, normal budgets; safe unattended toolset for
  both modes). Per-user `jn_…` Bearer tokens, server-managed conversations,
  SSE token streaming, cancel — all live and tested. `/api/stt` (server-side
  whisper slot) joined since the handoff and is what the app uses.
- v1 decisions (deviate from the old handoff, deliberately): **native
  Compose app** (not a WebView wrapper), **server-side STT** via `/api/stt`
  (not on-device whisper.cpp), **Android built-in TTS** (no server TTS HTTP
  endpoint exists — omnivoice is an agent tool; on-device Piper stays an
  option). Push-to-talk, no barge-in, new conversation per session.
- Not yet done: first Android Studio compile/device run, barge-in,
  conversation history browser, attachments, media playback.

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

### LiteLLM split: locals direct, cloud via Bifrost (2026-09-23 discussion)

Full litellm removal isn't worth it (cloud-provider unification is its
real value), but the local-first split is: brain/specialist/embed/rerank
talk **directly to their llama.cpp ports** (the slot machinery already
knows them — kills the proxy hop on the hot path plus the cache/fallback
footguns where they actually fire), litellm shrinks to a cloud-only
gateway. If cloud stays OpenRouter-shaped, **Bifrost** (Go, Apache 2.0,
single binary, ~11 µs overhead) is the candidate gateway — replacing both
litellm and its 100-pin Python venv. Helicone is observability, wrong
shape; Kosong (MoonshotAI) is an in-process SDK, wrong layer. Migration
notes: re-do the per-model cache opt-outs + fallback config in Bifrost
terms, re-validate judge/council/eval paths. Trigger: the next time the
litellm hop or a proxy footgun costs us a debugging session.

### Audit 2026-09-23 (Claude Opus) — leftovers

Everything else from that audit shipped same-day (see CHANGELOG
Unreleased). Remaining:

- **#12 web layer shape** (deliberately deferred, do opportunistically):
  routes_admin/routes_run/routes_eval `register()` closures → APIRouter +
  Depends; split admin.html's ~3,200 lines of inline JS per tab. Only when
  a module is touched anyway.
- ~~First guard ablation run~~ **DONE 2026-10-08** (see docs/brain-bakeoff.md
  "First guard ablation"): 70.7% ON vs 67.2% OFF over the 58-case delta set;
  6 rescues vs 4 interference, verify_arm is both top rescuer and main
  interferer → tuning candidate, not retire. Follow-ups: trace-dig
  verify_arm's intervention in the 4 hurting runs (premature-verification
  hypothesis), then a scope/threshold tune + re-ablation; schedule the
  ablation monthly from here. Also: benchmark UI has no `guards_off` input
  yet (API-only).
- **Ruff next stages**: B904 (44 raises without `from`), S110 (41
  try/except/pass), ASYNC240 (51 blocking Path calls in async — needs a
  to_thread pass).

## Parked (revisit only if …)

### ADRs + design-discipline skills

`docs/adr/NNNN-*.md` decision records and the `codebase-design` /
`domain-modeling` / `grill-with-docs` skills from the Matt Pocock
collection — adopt only if CONTEXT.md (the root glossary) actually drifts.

### Browser voice I/O (STT/TTS) — partially un-parked

Update 2026-09: **STT came back to core** (whisper.cpp helper slot, mic
dictation in the composer — see the Voice section above). Only TTS remains
parked, per that section. Original parking note below for the TTS shapes.

Browser mic dictation (whisper.cpp) + spoken replies (piper) were built
(2026-07: endpoints /api/stt + /api/tts, mic/speak UI, admin Voice pane,
then STT/TTS as slotted presets with a managed whisper process) and then
reverted — voice was not needed and too complicated to include cleanly.
The text-in/text-out `/api/voice` channel for native clients (Android)
predates all that and is unaffected. If voice ever comes back, the revert
commits live in the pre-squash history (search the log for "voice"), and
Orpheus-3B (GGUF via llama.cpp + SNAC decoder) remains the
high-quality TTS option over piper.

### Windows bundle (PyInstaller .exe) — 2026-09-30 assessment

Feasible in theory; the split is "Python freezes fine" vs "Linux
assumptions are the real work". Priority order if ever picked up:

1. **WSL2 first** (zero code): full stack runs today, CUDA passes
   through — document this as the Windows path before building anything.
2. **"Windows lite" bundle** (days, not weeks): PyInstaller/Nuitka freeze
   of the orchestrator + LiteLLM proxy, a launcher that opens the
   browser at localhost:8071 (or pywebview for a native window), and
   prebuilt llama-server.exe (CUDA/Vulkan) fetched like quickstart.sh
   does on Linux. The process manager already launches bare binaries
   (jevify), so swapping works without start-model.sh.
3. **Full parity: probably never worth it** — the hard Linux shape is
   sandboxing (code.run = firejail + podman devbox; Windows lite would
   be confirmation-only) plus bash setup scripts, systemd units, POSIX
   shell-outs in tools, and a Windows preset variant set (no ROCm).

### Small-brain context/thinking tuning (brain-dependent)

`orchestrator.context_tokens` (262k) and `reasoning_budget_tokens` (0 =
uncapped) are sized for the current cybertiel-35b-a3b brain — the
4B-class tuning from the Sept audit (≈32k context, thinking cap near half
of max_tokens) does NOT apply now. Revisit only when a small brain sits in
the slot again; measure with `scripts/route_bench.py` + the bakeoff, don't
guess.
