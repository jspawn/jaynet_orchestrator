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

View and edit the active gate prompt ("edit source"); an edit applies to the
next run. The shipped `prompts/orchestrator-gate.md` stays pristine — a live
edit (here, or an accepted eval prompt-tweak) writes an **overlay** in the
data dir that wins while present, so a deploy never conflicts with it. The
tab shows which layer is active and offers **Revert to shipped**.
Reasoning/`<think>` handling is automatic. This is the single most leveraged
knob in the system — small prompt changes beat big ones.

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
are disabled inside eval runs. The toolset is the unattended one with two
deliberate exceptions: the sandbox-confined write tools (`fs.write`/`fs.edit`)
run auto-approved against the per-case sandbox so cases can exercise real
write flows, and cloud `llm.call` stays available but auto-denied, so
privacy-gate cases test the real approval gate and the model's fallback —
every other confirmation-gated tool stays excluded. Runs are hermetic too:
the memory/RAG stores are redirected into the per-case sandbox, so a suite
can neither pollute real memory nor pull it into a judge transcript.

The judge is state-aware: next to the transcript it sees the run's available
tools, the live system prompt, the descriptions of rubric-relevant and called
tools, the bodies of the skills the agent actually loaded, and a config slice
(eval, budgets, loop guard, architect threshold, privacy/confirmation) — so
it cannot propose a prompt tweak for wording the prompt already contains, or
a fix for a tool the run never exposed (a rubric-required tool missing from
the toolset is also flagged by the deterministic checks as a case/toolset
problem, not an agent one).

The tab has four sub-views:

- **Cases** — the case list with each one's latest pass/score, the run bar
  (run the selected case or all cases carrying a tag), and the results table.
- **Statistics** — KPI cards, a daily pass-rate/score trend graph, per-case
  flakiness, and an A/B period comparison. Results record the brain alias, so
  a regression can be spotted per model.
- **Proposals** — the gated improvement inbox (below).
- **Benchmark** — head-to-head model/parameter shootouts. A **variant** is a
  label + model alias (blank = the current brain) + sampler overrides + a rep
  count; "same model, three temperatures" is just three variants on one alias.
  Run plays the chosen case/tag under every variant × reps sequentially and
  records each result under the variant's label. Compare aggregates the
  recorded results per label into a per-case matrix (pass rate, avg score,
  cost, elapsed) plus an overall row. Sampler semantics: variant keys win,
  unset keys fall back to `orchestrator.sampling` config — this holds for
  cross-model variants too (`sampling_force` opt-in). Pin `temperature: 0`
  and a fixed `seed` for repeatability — but treat it as best-effort
  (continuous batching and cloud providers still wobble), which is what the
  reps and pass rates are for. Labels share the namespace with recorded brain
  aliases: naming a variant `local-orchestrator` merges ordinary runs
  recorded under that alias into its column, so pick distinct labels (e.g.
  `brainA-t0`). A benchmark-wide cost ceiling
  (`eval.benchmark_max_cost_usd`, default $10) stops remaining reps across
  all suites. Comparing several *local* presets means swapping the served
  model between runs (manually, or pre-registered `serve.start` aliases);
  cloud aliases just work.

![Admin → Eval: the case list with each one's latest result, the run bar and the results table](../screenshots/admin-eval.png)

![Admin → Eval → Statistics: KPI cards, the overall pass-rate/score trend and per-case flakiness](../screenshots/admin-eval-stats.png)

A failed case produces a **proposal** (WHAT/CAUSE/FIX, classified
prompt-tweak / skill-tweak / tool-description / config / bad-test /
bug-for-dev, with a structured `target` + `proposed_content` where the
fix needs precision) in the inbox — nothing auto-applies. Accept applies to
the **custom layer only**, so builtins stay pristine and deploys never
conflict: a prompt-tweak extends the gate-prompt **overlay** (see the Prompt
tab), a skill-tweak appends to the skill's custom-layer copy (copying the
builtin skill down first), a tool-description replaces the description via
`custom/tool-overrides.yaml` (live + on boot), a config proposal sets a
whitelisted behavioural knob through the normal config-override path, and
bug-for-dev writes a ready-to-paste issue. Repeats are deduplicated, and
each artifact caps at 5 accepted tweaks — then consolidate the bullets into
the prose before accepting more. Cases
export/import as `.jaypack` via Studio. From chat, the agent can self-test
with `eval.run` / `eval.list` / `eval.report` — a nightly suite is just a
scheduled prompt (`schedule.add` → "run eval tag nightly"). Flagged sessions
can be turned into new cases with **make test** in the Flags tab; the flag
dialog's "include private context" checkbox (default off) controls whether
message text may feed that draft, and the draft is written by a local model
only — flagged content never leaves the box.

## Backup

Download a full data-dir backup as `.tar.gz`, or restore one — restoring
**overwrites all current data** (chats, users, presets, wiki, uploads,
projects) and needs a service restart afterwards. What's inside and what
migrates on upgrade: [upgrading.md](upgrading.md).
