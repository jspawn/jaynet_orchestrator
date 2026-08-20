# Admin console reference

Everything an admin can see and change, tab by tab. The console is
admin-only; regular users get the chat, the account menu and nothing else.
Deeper material lives behind the links at the end of each section.

## Status

Health at a glance: service version/uptime/active runs, the LiteLLM proxy
state, database sizes, RAM/VRAM/temps per GPU, and **Recent runs** — click
one for the step-by-step trace. This is the first stop when something feels
off. The JayNet web console and the LiteLLM proxy rows carry a **restart**
button (whitelisted user units; a console self-restart drops the page —
reload after a few seconds). → [operations.md](operations.md)

## Processes

The managed model servers as cards: live state, VRAM, start / stop /
restart, and an auto-refreshing log tail per server. Model crashes show up
here in red with their last lines. → [llama-ops.md](llama-ops.md)

## Presets

**Download from HuggingFace** sits on top: enter a repo id
(`bartowski/Qwen2.5-7B-Instruct-GGUF`), list its .gguf files with sizes
(chat templates shipped as `.jinja` are listed too, marked "template"),
download with live progress (cancel/dismiss included), then **create
preset** opens the editor prefilled from the finished download — name,
alias, next free port, a .conf skeleton with the right `MODEL_PATH`, and a
VRAM estimate. When the same repo ships an `mmproj*.gguf` or a `.jinja`
template, the skeleton already wires `MMPROJ`/`MMPROJ_OFFLOAD` /
`TOOLS_TEMPLATE` for it (the note tells you if that sibling still needs
downloading). This is the GUI twin of `scripts/pull-model` (both share
`runtime/hf_pull.py`); jobs live in process memory, so a restart forgets
them — leftover `.part` files older than an hour are swept from the models
dir on startup. Gated repos (or a raised rate limit): set `HF_TOKEN` in the
service env; both the GUI downloader and the CLI send it.

The model catalog (one row per servable model), the **boot model slots**
(which preset each managed process boots — any slot but brain can be
**(none)** to run without it; specialist2/3 ship empty), and the **cloud
models** editor — the
`llm.call` escalation path: alias, provider model, api base, key as an
*env-var name* (the pill shows whether it's set), $/1M tokens in/out,
thinking default, fallbacks, role shown to the brain. Saving re-renders the
proxy config; the repo's `litellm.yaml` stays the pristine seed.

The preset editor's launch flags are a **structured form** (one field per
key `start-model.sh` reads, with file pickers for model/mmproj/template);
the raw `.conf` stays available behind the **advanced** toggle. **Browse
model files…** opens the models dir read-only — ★ marks files a preset
references, and **Make preset from selected** drafts a preset for the
picked GGUF — and each llama-server binary (Admin → Processes) has a
**help** button showing its `--help` output.

Rows with **remote** enabled adopt an already-running OpenAI-compatible
server instead of launching one: an **endpoint** (bare host + port field,
or a full URL — a URL that carries its own port locks the port field), a
**backend** label (`llama`/`vllm`/`ollama`/`openai`) and **capability**
overrides (vision/thinking) for everything the probes can't see.
→ creating presets and contracts: [llama-ops.md](llama-ops.md#creating-and-editing-presets),
adopted servers: [models.md](models.md#adopt-existing-server),
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
  values, with a one-line explanation under each setting's label.
  Overrides are highlighted, persist across restarts and apply
  immediately; blanking a field resets it to the YAML default. The file
  stays the seed — the DB layer wins while set. The section-by-section
  map: [configuration.md](configuration.md).

## Tools

Globally enable/disable any tool for all users. A disabled tool is never
offered to the model — the strongest gate short of deleting code. Per-run
allowlists (`orch --tools`, quick settings) layer on top for a single run.
Every tool shows its one-line description inline (the same text the model
reads; the filter matches it too). The full list: [catalog.md](catalog.md).

Below the grid, **MCP servers** manages the external Model Context Protocol
servers the `mcp.*` tools bridge into the chat: stdio (a command JayNet
launches) or streamable-HTTP (a LAN/remote endpoint), a per-call confirm
toggle, per-server timeout, and a Test button that connects and lists the
server's tools. Saves apply live and persist as the `tools.mcp.servers`
config override — the first save takes over from any YAML-defined entries,
and deleting *all* servers falls back to the YAML definitions. Two form
limits to know: args are split on spaces and env entries on commas, so
values containing those characters need the YAML path.

## RAG

Collections with sources, chunk counts and size; delete per collection or
empty the store entirely. Ingestion itself happens through the `rag.*`
tools in chat, not here. → [architecture.md](architecture.md)

## Studio

Build skills, chains, connectors and tools in the browser; AI-assisted
drafting, validation, `.jaypack` sharing. → [studio.md](studio.md)

## Plugins

Lists every discovered plugin (repo builtins + installed ones under
`<data>/plugins/`) with state (loaded / disabled / unavailable incl. the
missing pip packages), what it provides (tools, skills, hooks, routes), and
an enable/disable toggle. Toggles persist but load at startup — **restart
the service** afterwards. Details and the plugin-writing guide:
[plugins.md](plugins.md).

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
  A running suite/benchmark can be **cancelled**: the case in flight finishes
  and is recorded, every later case is skipped, and the summary is marked
  cancelled. **Scheduled runs** below the run bar fire a suite unattended on
  an interval (e.g. `tag:web` every 24 h) through the same suite path —
  results land in the ledger tagged with the JayNet **version**, so a
  regression after a release or brain swap becomes a number. A schedule is
  skipped while any suite is running, and auto-disabled if its case/tag
  disappears.
- **Statistics** — KPI cards, a daily pass-rate/score trend graph, per-case
  flakiness, and an A/B period comparison. Results record the brain alias, so
  a regression can be spotted per model. The default view counts live runs
  only — benchmark reps are flagged and never move these numbers; the brain
  dropdown scopes every statistic (and the per-case trend drilldown) to one
  variant label.
- **Proposals** — the gated improvement inbox (below).
- **Benchmark** — head-to-head model/parameter shootouts. A **variant** is a
  label + model alias (blank = the current brain) + sampler overrides + a rep
  count; "same model, three temperatures" is just three variants on one alias.
  Run plays the chosen case/tag under every variant × reps sequentially and
  records each result under the variant's label. Compare aggregates the
  recorded results per label into a per-case matrix (pass rate, avg score,
  cost, elapsed) plus an overall row. The leading label gets a ★ winner bar
  with a one-click **route it** control — assign the winning preset to a
  slot (the dropdown preselects the preset whose alias matches the label;
  restart the process to apply). Sampler semantics: variant keys win,
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

## Flags

Two tables:

- **Flagged sessions** — chats users flagged for debugging. The log is
  privacy-safe by construction: message texts and tool args/results are
  stripped; you see structure (tool names, errors, iterations, timings),
  never content.
- **Watchdog reports** — automatic post-mortems: runs that ended
  stuck/error/stalled (or with heavy loop-guard churn) get a local-brain
  analysis — what happened, likely cause, one suggested fix.

## Users

Add users (with admin flag), reset passwords, delete; role, 2FA state and
created date per row. Below: usage per user (runs, errors, tokens, cost,
last active). Per-user budgets live in each user's account menu; flagged
sessions and the privacy model: [security.md](security.md).

## Backup

Download a full data-dir backup as `.tar.gz`, or restore one — restoring
**overwrites all current data** (chats, users, presets, wiki, uploads,
projects) and needs a service restart afterwards. What's inside and what
migrates on upgrade: [upgrading.md](upgrading.md).
