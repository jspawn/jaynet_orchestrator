# Harness Lessons — Architecture Brief

Lessons borrowed from leading agent harnesses (Claude Code, Codex, OpenHands,
mini-SWE-agent) and how each maps onto JayNet. Ordered by value-per-effort;
item 1 is implemented first.

## 1. Bash-only eval control (mini-SWE-agent style) — IMPLEMENTED

**Goal:** an ablation arm that runs the same eval cases with only `code.run`
(bash), no JayNet tools, skills, or gate prompt — so we can measure what the
harness actually buys. Shipped as two benchmark-variant fields:
`tools_only` (exact tool restriction, intersected with the unattended-safe
set) and `base_system` (whole-prompt swap for `prompts/<name>.md`), plus the
shipped `prompts/eval-bash-agent.md` control prompt and the *Tools only* /
*Base prompt* columns in the benchmark UI.

**What exists:** the benchmark variant mechanism runs end-to-end:
`EvalBenchmarkVariant` (`web/models.py`) → validation in `web/routes_eval.py` →
`eval_runner.run_suite(variant=...)` → stored as `brain=<label>, benchmark=1`
→ compared via `/api/admin/evals/benchmark/compare`. The `harness: "brain"`
variant already strips delegation tools, `guards_off` disables named rails, and
`runtime.run(tools=[...])` is a hard allowlist the loop refuses to widen.

**Design — two variant fields:**

- `tools_only: ["code.run"]` — applied in `_setup_case_run`
  (`runtime/eval_runner.py`) next to the stub-tool and `harness=="brain"`
  filtering; names validated against the registry in `routes_eval.py`.
- `base_system: "eval-bash-agent"` — a prompt name resolved against `prompts/`
  and passed to `runtime.run(base_system=...)`, which the loop already
  supports. Without it the bash-only arm would still see the gate prompt
  advertising tools it does not have. Ships as
  `prompts/eval-bash-agent.md` (~50 words).

**Caveat:** cases with `must_use_tools` on removed tools deterministically
fail — meaningful for the comparison; `check_expectations` already
distinguishes "not available" from "attempted and failed".

## 2. Versioned, typed client/run protocol

**Goal:** a stable contract for the Android app and future clients — before a
second client grows against ad-hoc dicts.

**What exists (~60% of the substrate):** every SSE event passes one envelope
`{v:1, run_id, seq, ts, type, iteration, data}` (`runtime/loop.py` `emit()`);
EventBus replay with `Last-Event-ID`; pydantic request models; per-user `jn_`
API tokens; `docs/api.md` already declares a stability policy scoped to ~8
routes. Missing: typed event/response schemas, URL versioning, durable
server-side threads (history is client-managed; runs die 120 s after
finishing).

**Design — three non-breaking increments:**

1. **Type the event stream.** Discriminated-union `RunEvent` in
   `web/models.py` covering the ~25 event types, enforced at the single
   funnel (`emit()` in `runtime/loop.py` + the fast-path emitters in
   `routes_run.py`). JSON Schema via FastAPI → codegen for Kotlin.
2. **Mount `/api/v1/...`.** New APIRouter alongside existing routes in
   `routes_run.py`; old paths keep working. `/v1` is where typed responses
   are guaranteed.
3. **Server-side threads.** `POST /v1/threads`, `POST /v1/threads/{id}/turns`,
   `GET /v1/threads/{id}/events`; ChatStore and current-chat sync are the
   natural home. Build when Android needs cross-device resume, not before.

## 3. Acceptance ledger + clean-state verification

**Goal:** for long autonomous work, a machine-readable record of what was
promised and what was verified — replacing "the model says it's done".

**What exists:** verify gate with sandboxed checks, vacuity detection, and
red→green baseline discrimination (`runtime/verify.py`); authored checks for
delegations (`tools/specialist/delegate.py`); `[must]` requirements gating
final answers (`runtime/todos.py` + `RequirementsGuard`); `notes.md`
write-through ledger (`tools/agent/note.py`); `open_must` in the run result.

**Design — extend, don't add:**

1. **Ledger = requirements with checks.** Each requirement can carry
   `check: <cmd>` and `verified: bool`, validated with the existing
   `run_authored_check` machinery. Stored as `ledger.json` in `work_root`,
   following the `notes.md` write-through pattern — shared with sub-agents,
   surviving compaction.
2. **Per-increment loop.** Marking a todo done triggers its check; the
   final-answer gate requires all `[must]` entries `verified: true`, not just
   `done`. The `/goal` supervisor's `| check: <cmd>` done-gate generalizes to
   this.
3. **Clean state per increment.** The delegate's git plumbing
   (`_snapshot_tree_ref`, isolated worktrees, `_worktree_report`) provides
   commit-or-discard per increment; commits stay confirmation-gated.

## 4. Prompt-cache measurement + per-model harness profiles

**Goal:** (a) know the cache-hit reality, (b) one versioned object per model
for template/reasoning/compaction/sampling behavior.

**4a — telemetry: IMPLEMENTED.** Every model turn records `turn_s` (whole
exchange) and `ttft_s` (streaming path only) from `runtime/model_client.py`
into the `model_turn` trace event, which also carries
`cache_hit_ratio = cached_tokens / prompt_tokens` (from llama.cpp's
`prompt_tokens_details` via LiteLLM). Eval results store a tokens-weighted
`cache_hit_ratio` column (judge/driver calls excluded) and the benchmark
Compare view shows a `% cache` figure per variant cell and overall row.

**What exists:** the prompt prefix is stable by design (frozen tool selection,
trailing volatile fragments); `cached_tokens` already flows from llama.cpp
through usage into `budget` and the `model_turn` trace event; presets.db
versions launch-time knobs. Per-call per-model behavior is scattered:
think-strip families hardcoded in `runtime/model_client.py`,
`reasoning_budget_tokens` global, compaction thresholds global.

**Design:**

1. **Telemetry first.** Extend the `model_turn` event with TTFT, turn
   duration (timing hooks exist in `_model_turn_stream`), and
   `cache_hit_ratio = cached_tokens / prompt_tokens`. Aggregate in
   eval_runner; show a cache column in eval/benchmark views. Optionally poll
   llama.cpp `/slots` per preset port for prompt-processing ms.
2. **Model profile object.** Anchored in the existing presets DB (no new
   store): identity/routing + launch config (already there), plus the
   currently-global knobs moved per-profile — think-strip family,
   think-switch eligibility, reasoning budget, compaction thresholds, anchor
   placement, per-role sampling. Runtime reads profile → falls back to
   current globals → behavior unchanged for unconfigured presets.

## Sequence

| # | Item | Effort | Depends on |
|---|------|--------|-----------|
| 1 | Bash-only eval control | done | — |
| 4a | Cache telemetry | done | — |
| 3 | Acceptance ledger | medium | — |
| 4b | Model profiles | medium | 4a |
| 2 | Protocol v1 (typed events → /v1 → threads) | incremental | before Android grows |
