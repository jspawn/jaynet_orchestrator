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

**Stages 1+2: SHIPPED.** The event stream is typed by `runtime/protocol.py`:
a registry of every event type (44) with its required/optional `data` fields,
one `build_envelope` as the only envelope producer (the loop's emit seam plus
every web fast-path literal now goes through it), and a `check()` at the seam
that warns once per distinct violation in production and raises under
`STRICT` — a fake-model loop run in `tests/test_protocol_v1.py` is the
meta-guard against unregistered emit sites. `/api/v1/*` is mounted
(`web/routes_api_v1.py`): `POST /api/v1/runs` (slash commands stay a
`/api/chat` feature → 422), `GET /api/v1/runs/{id}` (trace-store status;
`final_answer` owner-only), `GET /api/v1/runs/{id}/events` (SSE, adds an
`?after_seq=` alternative to `Last-Event-ID`), `POST …/cancel|approve|answer`
— thin wrappers over the same launcher/access-gate/providers, and the
unversioned paths remain supported aliases. Two deliberate deviations from
the original sketch: the registry is a plain dict-spec module in `runtime/`
rather than a pydantic discriminated union in `web/models.py` (the loop must
validate without importing the web layer, and warn-only validation needs no
framework), and `/v1` is a sibling route module rather than an APIRouter
inside `routes_run.py` (the launcher closure is shared via app state, same
pattern as `goal_kick`). **Stage 3 (server-side threads: `POST /v1/threads`,
`…/turns`, durable cross-device resume) stays unbuilt — not before Android
grows the need;** ChatStore and current-chat sync remain its natural home.

**What exists:** every SSE event passes one envelope
`{v:1, run_id, seq, ts, type, iteration, data}` (`runtime/loop.py` `emit()`);
EventBus replay with `Last-Event-ID`; pydantic request models; per-user `jn_`
API tokens; `docs/api.md` declares the stability policy and now documents
Protocol v1. Still missing: durable server-side threads (history is
client-managed; runs die 120 s after finishing).

## 3. Acceptance ledger + clean-state verification

**Goal:** for long autonomous work, a machine-readable record of what was
promised and what was verified — replacing "the model says it's done".

**IMPLEMENTED.** Todo items and requirements can declare a machine check
(`check` field or `| check: <cmd>` suffix — the `/goal` convention, reused).
The harness runs it through the verify sandbox (vacuity guard included) when
the item is marked done, or on demand via `todos action=verify`, and records
a harness-set `verified` flag; the requirements gate then demands a checked
`[must]` be **verified, not just done**, before the finish is accepted. The
plan writes through to `<workspace>/ledger.json` on every change (the
notes.md pattern); `/goal` iterations rehydrate it — only into an empty list
and only when the ledger's goal criterion matches, so unrelated runs never
inherit a stale plan. The ToDos panel and chat feed show check state
(✓/✗/…) via a `ledger_check` event. Opt-in clean state
(`agent.ledger_commits`): on git workspaces a checked item going `working`
snapshots the pre-increment tree (throwaway index), its check must fail on
that baseline (red→green discrimination — the delegate's authored-check
machinery, now shared in `runtime/verify.py`), and each verified increment
is offered as a confirmation-gated `ledger: <title>` commit. Kill switch:
`agent.ledger_checks: false`. Tests: `tests/test_ledger.py`.

**What exists:** verify gate with sandboxed checks, vacuity detection, and
red→green baseline discrimination (`runtime/verify.py`); authored checks for
delegations (`tools/specialist/delegate.py`); `[must]` requirements gating
final answers (`runtime/todos.py` + `RequirementsGuard`); `notes.md`
write-through ledger (`tools/agent/note.py`); `open_must` in the run result.

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

**4b — model profiles: IMPLEMENTED.** A preset can carry a JSON `profile`
column (presets.db migration, validated on write, edited as JSON in the
preset editor) with per-model overrides for: `think_strip` (which
chain-of-thought markup family `runtime/model_client.py` strips/splits —
qwen3 | gemma4 | both | none; the historical hardcoded both-families
behavior is the default), `reasoning_budget_tokens` (wins over the global,
explicit 0 disables), `compaction` + `anchor` (layered into
`parse_run_settings`), `sampling` (the preset's own sampler pins — for a
non-default model they apply alone, preserving the brain-only sampling
invariant), and `role_temperature` (per-tag fallback over the global table
for children delegated to that model). Resolution rides the config layer
(`profile_for(config, alias)` over `config["models"]["presets"]`), so admin
edits hot-apply like every other preset change; precedence everywhere is
run overrides > preset profile > global config > built-in default.
Think-switch eligibility was already per-preset (`caps.thinking`/`backend`).
Tests: `tests/test_model_profiles.py`.

## Sequence

| # | Item | Effort | Depends on |
|---|------|--------|-----------|
| 1 | Bash-only eval control | done | — |
| 4a | Cache telemetry | done | — |
| 3 | Acceptance ledger | done | — |
| 4b | Model profiles | done | 4a |
| 2 | Protocol v1 (typed events → /v1 → threads) | stages 1–2 done; stage 3 (threads) not before Android grows | before Android grows |
