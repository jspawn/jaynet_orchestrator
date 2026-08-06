# Admin console reference

Everything an admin can see and change, tab by tab. The console is
admin-only; regular users get the chat, the account menu and nothing else.
Deeper material lives behind the links at the end of each section.

## Status

Health at a glance: service version/uptime/active runs, the LiteLLM proxy
state, database sizes, RAM/VRAM/temps per GPU, and **Recent runs** — click
one for the step-by-step trace. This is the first stop when something feels
off. → [operations.md](operations.md)

## Processes

The managed model servers as cards: live state, VRAM, start / stop /
restart, and an auto-refreshing log tail per server. Model crashes show up
here in red with their last lines. → [llama-ops.md](llama-ops.md)

## Presets

The model catalog (one row per servable model), the **model slots** (which
preset each managed process boots), and the **cloud models** editor — the
`llm.call` escalation path: alias, provider model, api base, key as an
*env-var name* (the pill shows whether it's set), $/1M tokens in/out,
thinking default, fallbacks, role shown to the brain. Saving re-renders the
proxy config; the repo's `litellm.yaml` stays the pristine seed.
→ creating presets and contracts: [llama-ops.md](llama-ops.md#creating-and-editing-presets),
placement rules: [model-placement.md](model-placement.md)

## Prompt

View and edit the active system prompt ("edit source"). Takes effect on the
next run; reasoning/`<think>` handling is automatic. This is the single
most leveraged knob in the system — small prompt changes beat big ones.

## Config

Two sections:

- **Default run budget** — the ceilings applied to every run that doesn't
  set its own. Toggle off = unlimited. Settings layer: these defaults ←
  per-user account defaults ← per-run controls; upper layers can only
  tighten, never loosen.
- **Runtime configuration** — a filtered editor over `runtime.yaml`
  values. Overrides are highlighted, persist across restarts and apply
  immediately; blanking a field resets it to the YAML default. The file
  stays the seed — the DB layer wins while set.

## Tools

Globally enable/disable any tool for all users. A disabled tool is never
offered to the model — the strongest gate short of deleting code. Per-run
allowlists (`orch --tools`, quick settings) layer on top for a single run.
The full list with one-line descriptions: [catalog.md](catalog.md).

## Users

Add users (with admin flag), reset passwords, delete; role, 2FA state and
created date per row. Below: usage per user (runs, errors, tokens, cost,
last active). Per-user budgets live in each user's account menu; flagged
sessions and the privacy model: [security.md](security.md).

## Flags

Two tables:

- **Flagged sessions** — chats users flagged for debugging. The log is
  privacy-safe by construction: message texts and tool args/results are
  stripped; you see structure (tool names, errors, iterations, timings),
  never content.
- **Watchdog reports** — automatic post-mortems: runs that ended
  stuck/error/stalled (or with heavy loop-guard churn) get a local-brain
  analysis — what happened, likely cause, one suggested fix.

## RAG

Collections with sources, chunk counts and size; delete per collection or
empty the store entirely. Ingestion itself happens through the `rag.*`
tools in chat, not here. → [architecture.md](architecture.md)

## Studio

Build skills, chains, connectors and tools in the browser; AI-assisted
drafting, validation, `.jaypack` sharing. → [studio.md](studio.md)

## Eval

Behavioural tests for the agent itself (unit tests cover the plumbing): YAML
cases in `evals/` + the custom layer, each a scripted or adaptive multi-turn
conversation driven through the real agent loop and graded by a judge model
(`eval.judge_model`, falling back to `local-specialist`). Run single cases or
bulk by tag; results, pass-rate trends and judge notes are kept in
`eval.db`. The only budget is $ (`eval.max_cost_usd` per case,
`eval.suite_max_cost_usd` per bulk run) — iteration/wall-clock/token ceilings
are disabled inside eval runs, confirmations auto-deny, and the toolset is
the unattended one (no gated or remote tools), so a gated call tests the
fallback path.

A failed case produces a **proposal** (WHAT/CAUSE/FIX, classified
prompt-tweak / tool-description / config / bad-test / bug-for-dev) in the
inbox — nothing auto-applies: accept appends prompt tweaks to
`prompts/orchestrator-gate.md` (git-visible) or writes a ready-to-paste issue
for bug-for-dev; repeats are deduplicated. Cases export/import as `.jaypack`
via Studio. From chat, the agent can self-test with `eval.run` / `eval.list`
/ `eval.report` — a nightly suite is just a scheduled prompt
(`schedule.add` → "run eval tag nightly"). Flagged sessions can be turned
into new cases with **make test** in the Flags tab; the flag dialog's
"include private context" checkbox (default off) controls whether message
text may feed that draft.

## Backup

Download a full data-dir backup as `.tar.gz`, or restore one — restoring
**overwrites all current data** (chats, users, presets, wiki, uploads,
projects) and needs a service restart afterwards. What's inside and what
migrates on upgrade: [upgrading.md](upgrading.md).
