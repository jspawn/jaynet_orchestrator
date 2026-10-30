# Learning Guide — how LLM agents actually work

> A short theory companion to JayNet. It explains the ideas behind the
> code so that a curious newcomer can read the codebase afterwards, and an
> experienced reader can skip straight to the parts they don't know yet.
> Setup and operation live in [docs/](docs/); this file is about
> understanding. The code is the source of truth — this is the map.

Already know what an agent loop is? Skim [§3](#3-short-deep-dive) and
[§4](#4-links-for-more); the rest is groundwork.

---

**Two parts.** Part 1 is how JayNet works NOW — current design,
current config, current tools. Part 2 is how we got here — the
measured lessons and bakeoff history behind those decisions. Section
numbers are stable (docs and code cross-reference them), so Part 2
keeps its original §3.x numbering.

- **Part 1:** §1 Overview · §2 JayNet examples · §3 Short deep dive
  (§3.1-§3.14, §3.17-§3.19, §3.21-§3.29) · §4 Links + cheat sheet
- **Part 2:** §3.15 Three decisions worth stealing · §3.16
  Benchmarking the brain · §3.20 Show the budget

---

## Part 1 — how it works now

Current design, current config, current tools. A newcomer
reads this part first.

## 1. Overview

### 1.1 The completion API is stateless

Every interaction with an LLM is a single call: text in, text out. The model
remembers nothing between calls. When you "chat" with a model, the client
sends the *entire history* with every message:

```json
POST /v1/chat/completions
{ "messages": [
    {"role": "system",    "content": "You are helpful."},
    {"role": "user",      "content": "Capital of France?"},
    {"role": "assistant", "content": "Paris."},
    {"role": "user",      "content": "And of Germany?"}
]}
```

This is the single most important fact for understanding agents. An agent is
not a process that "thinks" — it is a **loop that reconstructs context and
re-asks the model** on every turn. The agent's "memory" is whatever the loop
chooses to include in `messages` next time.

### 1.2 Tool use is just structured output

Giving a model "tools" means:

1. You send tool *schemas* (JSON Schema descriptions) with the request.
2. The model, trained to recognize when a tool would help, replies with a
   structured payload like `{"name": "web.search", "arguments": {"query": "…"}}`.
3. **You** — the orchestrator — execute it and append the result as a message
   with `role: "tool"`.
4. The model continues: more tools, or a final answer.

The model never executes anything. It writes JSON describing what it would
like to happen; your loop decides whether to actually do it. **Agentic
behavior lives in the loop, not in the model.**

### 1.3 Four levels of agentic behavior

| Level | What it does | Examples |
|---|---|---|
| 0 | Single-turn tool use, then answer | weather app extracting a city name |
| 1 | Bounded reasoning loop: tool → result → tool → answer | JayNet, most function-calling demos |
| 2 | Sub-agents: specialized agents spawned for sub-tasks | Claude Code, AutoGen |
| 3 | Long-running autonomy: persistent state, external triggers | scheduled research agents |

Each level adds capability and blast radius. Level 1 is the sweet spot for
most things: predictable cost ceiling, debuggable, genuinely useful. JayNet
is Level 1 with a taste of Level 2 (the brain can spawn sub-agents with their
own loop, tools and budget).

### 1.4 Why build this yourself?

You could use a framework. But an agent loop is ~200 lines of code, and
understanding those 200 lines teaches you more than a thousand lines of
abstractions you don't control. Once you've built one, you can read any
framework's source and recognize every piece.

---

## 2. JayNet examples — the theory, visible

Each concept above has a place in JayNet where you can *watch it happen*.

**Statelessness → the trace.** Every run is logged step by step to
`trace.db` and shown in Admin → Status & Usage → Recent runs (and `scripts/orch
--trace <id>` on the CLI). Open any run: you'll see `model_turn`,
`tool_call`, `tool_result` events — the loop reconstructing context and
re-asking, exactly as §1.1 describes.

**Tool calls → ask and watch.** Ask the chat something like *"what's the
latest stable Linux kernel?"*. The brain calls `web.search`, reads the
result, maybe `web.fetch`es a page, then answers. In the trace you see the
model emitting JSON, the loop executing it, the result going back in as a
`tool` message (§1.2).

**Budgets → bounded loops.** Every run has hard ceilings: iterations,
wall-clock, cost, tokens — visible per user in the account menu and in
`config/runtime.yaml` (the shipped default leaves wall-clock off and leans
on a stall watchdog plus the other three). Hit one and the run ends loudly
with `budget_exceeded`, it never runs away. This is the discipline that
makes Level 1 safe to own.

**The privacy gate → taint.** Tools in private namespaces (files, RAG,
notes) *taint* the conversation. While tainted, calls to cloud LLM tools are
refused unless you explicitly opt in (`share_private`). Coarse, but actually
enforced — at the dispatcher, where the model can't paraphrase around it.
Try it: index a private note, then ask the brain to have a cloud model
summarize it without opting in, and read the refusal in the trace.

**Roles → a second boundary, same shape.** Admin-grade tools (`ops.run`,
`job.*`, `serve.*`, `model.use`, `git.push`, `mcp.call`, `studio.python` —
`security.admin_only_tools`) are hidden from tool selection
AND refused at dispatch for non-admin accounts. Hiding alone would fail the
day a model guessed the name; refusal alone would waste turns on refusals —
you need both, enforced in the dispatcher, not the prompt. `auto_confirm`
is likewise forced off server-side for non-admin sessions (chat, goals,
voice, schedules), and `/goal … | check:` is admin-only: the check command
runs unsandboxed as the service user, so planting one is an admin trust
decision, not a user convenience. Scheduling is the one tier with a per-user
dial: `schedule.add`/`schedule.remove` are normal tools, but the web layer
disables them per run unless the account is admin or has the
`schedules_allowed` flag (Admin → Users) — a scheduled run fires with the
owner's role, so an admin's schedule auto-confirms, a user's doesn't.

**Local brain, cloud as tools.** The orchestrator is a small local model;
cloud models (Claude, Gemini, …) exist as `llm.*` tools it may call (plus
two equally-gated exceptions: a cloud panelist in `council.debate`, and
`agent.spawn` targeting a cloud model). The cloud never sees your
conversation — each call gets a self-contained task description. Routing is
cheap and free locally; only real work costs money. That's the whole
local-first idea in one pattern.

**Presets → models as managed infrastructure.** The brain can list model
presets and load one mid-chat (`model.use`) — a coding specialist for a hard
patch, then back. On smaller hardware this is the key trick: one swappable
slot serves many finetuned experts, because only the one the current task
needs is loaded. Models are not fixed endpoints; the agent reconfigures its
own hardware. Admin → Models → Presets shows the catalog.

**Studio → the agent helps build its own extensions.** In Admin → Studio an
admin drafts skills (versioned know-how the brain loads on demand), chains,
declarative API connectors and Python tools — with AI-assisted drafting by
the local model, validated before save, shareable as `.jaypack`. Skills are
*progressive disclosure*: the standing prompt carries only each skill's
name and one-line description — the description is the trigger — and the
full instructions enter the context on an explicit `skill.load`, so the
library grows without growing every prompt. The theory bit: skills and
tools are just more structured context the loop can use
([docs/studio.md](docs/studio.md)).

**Chains → choreography you can read.** When the control flow is known in
advance, re-deciding it in the loop on every run is waste and variance. A
chain (`chains/*.yaml`) is a declarative pipeline — steps with explicit
tools and models, each one seeing `{{steps.previous.output}}` — run by
`chain.run`. Theory: fixed choreography where the plan is certain, free
loop where it isn't; the craft is knowing which situation you're in.

**Visible planning → the to-do rail.** Multi-step runs plan from an
explicit to-do list shown beside the chat, with live statuses (done /
working / failed / skipped). Beyond the UX, it's an architectural move:
the plan is *external state* the loop reads instead of re-deriving it from
an ever-longer transcript — and because it's visible, a human can correct
intent mid-run instead of after the bill arrives.

**Memory vs RAG → two answers to forgetting.** `rag.*` retrieves chunks of
documents *you* indexed; `memory.*` and `kg.*` are facts the agent
maintains *itself* across runs — full-text search plus a typed knowledge
graph. Same goal (the model remembers nothing), different ownership of the
knowledge.

**Verification → don't trust, check.** Where a real checker exists (tests,
linters), the loop wires decisions to it. Where none exists (summaries,
reports), `verify.score` grades with a continuous logprob-based score and
`verify.rank` keeps the best of several attempts — the difference between
declaring done and *being* done.

---

## 3. Short deep dive

### 3.1 Anatomy of one loop iteration

```
┌──────────────────────────────────────────────────────────────┐
│ 1. Budget tick (iterations++, time, cost, tokens)            │
│    → ceiling hit? end the run, loudly.                       │
├──────────────────────────────────────────────────────────────┤
│ 2. Model turn: POST the whole message history + tool schemas │
│    → either tool_calls = [...]   → step 3                    │
│    → or    content (no tools)    → final answer, exit        │
├──────────────────────────────────────────────────────────────┤
│ 3. For each tool call:                                       │
│    a. loop guard — same call 3×, no write since? refuse      │
│    b. privacy gate — remote LLM + tainted convo? refuse      │
│    c. hard timeout — one hung call can't kill the run        │
│    d. execute; append result as role:"tool"                  │
│    e. private tool? taint the conversation                   │
├──────────────────────────────────────────────────────────────┤
│ 4. Loop back to 1                                            │
└──────────────────────────────────────────────────────────────┘
```

The model picks *what* to do; the loop decides *whether to allow it and how
to surface the result*. Read `runtime/loop.py` twice — once you can trace
one iteration in your head, you understand agents.

The loop also watches for *no progress*: consecutive turns that change
nothing (only reads, searches, polls) earn escalating injections — "act
now" → "dumbest working version or delegate" → "produce or ask". The
measured lesson: **count product, not activity.** When todo-list updates
counted as progress, the brain hid in eleven consecutive planning turns;
fixed that, and it hid in re-running the same verification script fourteen
times. Both now count as no-progress — only turns that change the work
product (or wait on a real running job) reset the ladder. Any activity
metric a loop rewards, a hesitant model will eventually learn to farm.

### 3.2 Token economics

**Tokens compound.** Every iteration resends the whole history. A 10-turn
loop adding 2k tokens per turn sends ~110k input tokens, not 20k — cost
grows quadratically with loop length. That's why tools truncate results
defensively and large results get summarized before entering the context.

**Prompt caching breaks the compounding.** Unchanged prefix tokens (system
prompt + tool schemas, stable across turns) are reused instead of
reprocessed: llama.cpp keeps the KV cache hot between turns, and cloud
providers bill cached prefix tokens at ~10%. This is a *provider* feature,
not the proxy's — LiteLLM routes and reports the cached-token counts,
JayNet's budgets price them accordingly. One practical consequence:
anything inserted *early* in the prompt (a changing tool list, a growing
memory dump) busts the prefix and taxes every later turn — keep the front
of the prompt stable.

**Reasoning tokens are completion tokens.** A thinking model's `<think>`
block counts against the same `max_tokens` cap as the answer, so uncapped
thinking can eat the whole budget and return *nothing*. JayNet's answer is
mechanical, not hopeful: `REASONING_BUDGET` (→ `--reasoning-budget`) makes
llama.cpp force-close the think block at a set size, reserving the rest of
the cap for the actual reply — and when a turn still gets cut mid-reasoning,
the loop says so once instead of letting the model wonder why its turn
vanished.

### 3.3 Why small local models orchestrate fine

The orchestrator's job is routing: read the request, pick tools, read
results, decide next, finally answer. A modest model trained for tool
calling handles that; the heavy lifting (prose, deep reasoning, code) is
delegated to specialists *as tools*. Orchestration calls are free and
private; only the delegated work leaves the box or costs money.

### 3.4 The VRAM mental model (one paragraph)

A model server needs VRAM for weights + KV cache + overhead.
Weights = parameters × bytes-per-param (a 4-bit quant ≈ 0.6–0.7 B/param, so
a 30B model ≈ 18–20 GB). The KV cache grows with context length — quantizing
it (`q8_0`) roughly halves that. Everything else in this area (which quant
to pick, which flags matter, how to split GPUs) is operational detail:
[docs/models.md](docs/models.md) and [docs/llama-ops.md](docs/llama-ops.md).

### 3.5 Quantization in one box

GGUF quants compress each weight from 16 bits to ~4–8. The mechanism is
*affine block quantization*: weights are grouped into small blocks, each
block stores a scale and an offset, and the values become small integers.
K-quants (`Q4_K_M` & friends) refine this with super-blocks and **mixed
precision** — attention and other sensitive tensors keep more bits, FFN
weights fewer — which is why `Q4_K_M` is the default sweet spot rather than
the older uniform `Q4_0`. I-quants go lower still, calibrated with an
importance matrix measured on real data. Judge a quant by its perplexity
delta, not vibes; KV-cache quantization is the same trick applied to the
context.

### 3.6 RAG: retrieve, then rerank

Two-stage retrieval is the pattern behind `rag.*`. A **bi-encoder**
(embedding model) scores every chunk independently — vectors are
precomputed, so search is a fast cosine scan — then a **cross-encoder**
reranker reads query and candidate *together* to precisely order the top
few. Stage one is cheap and approximate, stage two exact and slow; each
covers the other's weakness, which is why you want both. The unglamorous
lesson: chunking and coverage matter more than which embedding model you
pick.

### 3.7 Sub-agents: the point is context isolation

`agent.spawn` runs a child loop with its own tools, budget and fresh
context. The win is not parallelism — it's that the child's twenty messy
search turns never enter the parent's context; only its distilled answer
comes back. Two safety invariants: the child's budget clamps to the
parent's remainder and reconciles back afterwards, and its tool set can
only ever be *narrower* than the parent's, never wider. Depth is capped —
sub-agents all the way down is a bill, not an architecture.

Two patterns build on top. `agent.fanout` is parallelism for real: map N
tasks over child loops concurrently, merge the distilled answers — partial
failure is reported as signal, only all-failed is an error. `council.vote`
is self-consistency: sample the same question N times at temperature and
take the majority answer, with failed samples abstaining and ties reported
rather than picked. Both charge the parent budget, and both route through
**strength tags** (`strength="coding"`): presets declare what they're good
at, one shared resolver picks the live specialist for the tag — so the
27B coder gets the coding whether the caller is a delegate, a spawn or a
fan-out child. Honest caveat the tool descriptions carry: same-model
children serialize on one GPU, and N samples cost N completions.

### 3.8 Compaction: when the window fills

Contexts fill, and two mechanisms answer that at two scales. *Inside a
run*, superseded reads of the same file slice become re-read pointers, while
old oversized results get stubbed. Execution logs retain their command, exit
code, head and error tail; recent and explicitly pinned results stay verbatim,
and the full evidence remains available through `trace.query`. *Across a chat*,
`/compact` summarizes the older history into a
continuity brief written as working notes to the assistant's future self —
goal, constraints, decisions with reasons, concrete state — and keeps the
last exchanges verbatim so the conversational flow survives. The theory is
§1.1 applied honestly: memory is whatever the loop includes, so compaction
is an editorial decision about what earns the tokens. Know the failure
mode: a summary is lossy, so anything that must survive *exactly* belongs
in a file, not in the transcript.

### 3.9 Four knowledge surfaces, one treaty

JayNet keeps knowledge in four distinct places, and confusing them is the
most common newcomer stumble:

- **`rag.*`** — chunks of documents *you* indexed (§3.6). Owned by you,
  retrieved per run.
- **`memory.*` + `kg.*`** — facts and a typed knowledge graph the agent
  maintains *itself* across runs. Owned by the agent, curated over time.
- **`graph.*`** (graphify plugin) — a static-analysis map of a *project*:
  files, symbols, dependencies — and, since the wiki extractor, the
  project's wiki pages as nodes. Neither memory nor RAG — a mirror of the
  code, rebuilt when files change (opt-in debounced auto-rebuild), marked
  dirty when stale. The doctrine it enables: query the graph before
  grepping. The surfaces also bridge: `graph.seed_kg` mirrors a project
  graph into `kg.*` (namespaced, provenance-tagged), and project-bound
  `rag.search` answers carry the graph neighborhood of their hits. And
  projects can start with knowledge already written down: `/charter`
  interviews a new project into existence and stores the answers as its
  wiki — which the extractor then turns into graph nodes.
- **Workspace files** — the dumbest and most durable surface: anything
  that must survive exactly gets written down.

The naming collision is real: `kg.*` (a knowledge graph about *your
world*) and graphify's `graph.*` (a graph of a *codebase*) are unrelated
structures sharing a word. Same goal everywhere — the model remembers
nothing — different ownership, freshness and query semantics.

### 3.10 RLM: context as a variable

Long documents break the loop's economics (§3.2): haul 200 pages into the
window and every later turn pays for them. The RLM pattern (Recursive
Language Models, arXiv 2512.24601) flips it: keep the bulk *outside* the
context as a variable — a staged file — and address it programmatically:
slice it with `code.run` (language=python), map budgeted one-shot **subcalls** over the
slices, reduce the partial answers yourself. The model sees pointers and
distillates, not bulk. In JayNet this is native, not a plugin:
`context.stage` + workspace files + `code.run` python mode + subcalls, with the
`long-document` skill teaching the doctrine. It is the same insight as
compaction (§3.8) aimed at input instead of history: the context window is
for thinking, not storage.

### 3.11 J-space: a deliberate inner workspace

The `j-space` skill is a different kind of extension — it adds no tools,
only discipline. Named after research on model internals that found a
privileged set of representations holding what the model is *poised to
say*, it teaches the loop to use that workspace deliberately: classify the
task (fast / full / loop), route to the module the task earns, keep a goal
alive through long mechanical stretches with a ledger file, and recover
from degenerating reasoning instead of doubling down. The transferable
idea: a skill can change *how* the loop thinks, not just what it can do —
progressive disclosure applied to reasoning itself. It loads on demand for
hard tasks; watch for its workspace ledger when a run gets long.

### 3.12 Plugins: extension without forking

Skills teach and tools act — but some extensions need both, plus hooks
into the loop and their own admin UI. That's a plugin: a directory with a
`plugin.yaml` manifest, tools, optional hooks, routes and skills — toggled
in Admin → Harness → Plugins, default-off, no core changes. Toggles apply live:
enable registers the plugin into the running process, disable removes
exactly what it added, so iterating on a plugin is a toggle cycle rather
than a restart (fresh pip dependencies excepted); a plugin may even ship
its own admin UI. Graphify is the
reference implementation and sets the bar: it ships staleness semantics
(the graph is a snapshot, not a truth) and a skill that teaches when to
query it. Benchlab is another — benchmark harnesses (Terminal-Bench,
GAIA) packaged the same way — and its existence matters more than its
content: a second plugin proves the interface right in a way one never can.
(Eight builtins ship now — graphify, benchlab, h5i, jev, clm, imagegen,
omnivoice for local text-to-speech, pageindex for vectorless long-PDF
retrieval; §3.18 covers what
jev's measured failure taught about routing.)
The design lesson mirrors the MCP note below: adopt structure when a
second real consumer earns it.

### 3.13 The supervised self-improvement loop

The eval harness closes a loop most agent projects leave open. A flagged
or stuck session becomes a regression *case* with one click; a suite run
replays cases through the real agent loop — not a mock — against the live
harness; a judge model turns failures into concrete *proposals* (prompt
edits, skill rewrites, tool-description fixes, config changes) that a
human applies or dismisses; acceptance appends a reversible snapshot to the
source brain's overlay, and the next suite measures the effect.
Statistics track pass rates, flakiness and trends; the benchmark tab
shoots candidate models against each other on the same suite before you
swap brains. In the Cases tab, rows click-select for the run bar and a
confirmed Run all plays the whole library. Two disciplines keep it trustworthy: proposals are
suggestions under human review, never self-applied, and benchmark
repetitions are labeled so they don't pollute the trend numbers. The
theory: an agent you can't regression-test is a demo.

### 3.14 Sandboxed execution: from firejail to containers

Every `code.run` command is untrusted code a model wrote, so where it
executes is a security decision, not a convenience. The baseline is
firejail: the command sees only the run's workspace and tmp, no network
unless granted. Its limit is the host itself — "compile this Rust" fails
when the host has no Rust. The **devbox** answers that without widening
the blast radius: opt in once and each run gets its own rootless podman
container from a toolchain image (Rust, Go, Node, C/C++, Java, .NET),
mounting exactly the same two roots firejail would — same confinement
shape, real compilers inside. Dependency caches live on shared volumes so
iterative builds stay fast, and the privacy rule holds *live*: network is
on for the registries, but the moment a run touches private data the
running container's network is cut mid-run, not just on the next start.
Podman missing or image not built → it falls back to firejail and says
so. The transferable lesson: degrade one-directional and safe, and make
the privacy rule a property of the mechanism, not of the prompt.

---

### 3.17 Procedures: distilled process, loop-enforced

Frontier models beat small ones on agentic tasks mostly by *process
discipline*, not knowledge — and process is distillable. A **procedure** is
a skill with a task-shape tag (`shape: multi-step-puzzle`, say) and a
checklist: the loop auto-loads the matching procedure at run start (small
brains rarely `skill.load` on their own), and its checklist feeds the
machinery instead of the prompt's hopes — the stall ladder nudges against
*its* undone steps, and the final-answer check asks whether they were done.
The general shape: write down how a good operator does a task class once,
then let the loop enforce it every time. Procedures are plain skills under
the hood, so they package and share like any other (jaypack).

### 3.18 Decision models: not every judgment is a generation

Some things an agent needs aren't text — they're *judgments with a fixed
answer space*: which specialist should take this request, is this segment
worth keeping, which team owns this ticket. A **decision model** (the
category TypeSafe's Jev opened, 2026) answers exactly that: state + typed
questions in, calibrated *probabilities* out, one forward pass, no
generated text to parse — so "how sure are we?" is a number you can set
thresholds on, not vibes in a JSON blob.

We tested it as a delegation classifier for the "models won't delegate"
problem (`plugins/jev`, a `route_request` hook on the loop's routing
seam), in two rounds on the same twenty real prompts. The open checkpoint
(Open-Jev 2B, local GPU) **failed**: trained on synthetic business
decisions, it called coding requests "general" at 0.79 confidence — our
keyword router won. The hosted proprietary model **aced it**: 0.92–1.00 on
coding and research, 0.99 on vision, chat correctly left unrouted, ~0.4 s,
fractions of a cent. So the *idea* is proven and the *open weights* aren't
there yet — and since routing via the cloud sends every request's text off
the box, we stayed with keywords until an open checkpoint trained on intent
routing lands. Two lessons generalize: test an idea with its strongest
implementation before rejecting it (the 2B's failure said nothing about
the approach), and a local-first architecture treats "which model sees the
request" as a privacy decision, not just a latency one.

### 3.19 The guard pipeline: rails you can measure

Every chapter above kept saying "the loop enforces it" — this section is the
*how*. A **guard** (rail) is a registered class the loop consults at a fixed
phase, over per-run state kept in one place (`RunState`). Three phases,
three registries: **pre-turn** guards fire at turn start (budget warnings,
the stall ladder), **post-tool** guards fire after each tool result
(failure streaks, the delegate nudge), and **final-answer** guards decide
whether a candidate answer is accepted or *bounced* back with a
reason (truncation, missing deliverable, verify-the-delegate). Some rails
live one layer down, at the dispatch gate itself — the role policy above,
and the repeat hard block (`loop_guard.hard_block_repeat_errors`: the 4th
identical (tool, args, error) failure is refused without executing).

Two mechanisms make this more than a tidy refactor. First, telemetry: every
guard application emits a uniform `guard_fired` event
(`{"name", "phase", "turn"}`), so each rail has a *fire rate* — and a rail
you can count is a rail you can A/B: benchmark variants accept
`guards_off: [names]` and replay the fixed case list with one rail
disabled, which is how you learn which rails pay and which only feel safe.
Second, economy: a bounce is a full model turn over a growing context, so
final-answer bounces are capped (`agent.max_bounces_per_answer`, default 3)
— at the cap the answer is accepted and the `bounce_cap` event names the
guard that was suppressed. The transferable lesson extends §3.15: once
every rail is a named, countable object, harness tuning stops being
folklore and becomes an ablation study.

Reading the counts is its own skill, and `scripts/eval-peek.py` does it
for you: every report closes with a per-guard table — fires, cases
involved, pass-rate-when-fired vs pass-rate-when-quiet. That split reads
the rails at a glance. A rail whose fires convert to passes is a
*rescuer* (first live reading: `requirements` and `procedure` at 100%
pass-after-fire, `verify_arm` netting +8 points). A rail whose
pass-after-fire sits at ~0 only ever sees doomed runs — `cap`,
`wrap_up`, `trunc` — which doesn't make it useless (it may be correctly
flagging the dying), but it means the rail never rescues anything: the
candidate list for the next `guards_off` ablation reads straight off
this table instead of being guessed.

Running the ablation is a form fill, not an API call (Admin → Eval →
Benchmark): one `baseline` variant with all rails on, one `no-<guard>`
variant per suspect rail, each with pinned sampling
(`{"temperature": 0, "seed": 42}`) and the same rep count. The *Without
guards* column completes guard names from the registered rails, and an
unknown name fails the variant with a 400 naming the culprit — an
ablation that silently ran with all rails on would fake the answer. Pick
the case tag that exercises the rail (e.g. `verify` for `verify_arm`),
fire, then read it twice: the Compare matrix for per-case pass-rate
deltas between baseline and ablated, and eval-peek's guard table for
what actually fired. Two discipline notes, both learned from real runs:
pinned sampling on llama.cpp is best-effort, so a single flipped case at
reps 3 is a model slip until the trace says otherwise (the toolset-trim
A/B's two negative flips were exactly that); and the ablation is a
*ritual*, not a one-off — rerun it monthly against whatever the
telemetry's rescuer/interferer split names next, because a rail that
paid against last month's brain may be dead weight against this month's.

---

### 3.21 Measured scheduling: pack by footprints, not estimates

For two years the swapper's unit of truth was `vram_gib` — a hand estimate
per preset, maintained by whoever wrote the `.conf`. Two things made that
the weak link: estimates drift the moment someone bumps `CTX_SIZE` (the KV
cache is usually the second-biggest allocation after the weights), and a
flat "any co-tenant on a pinned GPU conflicts" rule forced an eviction
even when both models would have fit side by side. The fix was to make the
footprint **data the machine collects about itself**: `model.measure`
hibernates every local GPU tenant (brain included — that's why it asks
first), loads the preset, fires one tiny probe completion at it, and
records the per-card VRAM delta plus the RAM delta onto the preset. The
scheduler then packs by those numbers: a co-tenant is only evicted when a
shared card genuinely can't hold `share + floor` more — co-tenancy is the
default when measured shares fit, eviction the fallback.

What the build taught us:

- **One tiny probe is enough.** llama.cpp preallocates the weights and the
  full-context KV cache at startup, so the footprint right after load is
  already the steady-state one — the probe only needs to touch the compute
  buffers. A 16-token "Say OK" against the preset's own LiteLLM alias
  yields a stable measurement; no benchmark run required.
- **Restore what was actually live, and survive cancellation.** The
  hibernation plan records the preset *probed on each slot* (a previous
  swap may have changed it), never the boot default — restoring the boot
  default would "fix" the box into the wrong state. And the restore runs
  on `BaseException`: cancel the measurement mid-load and the box still
  comes back, measurement server down first (it may hold the very port and
  cards the hibernated models retake), brain before the cancel propagates —
  the brain is the current run's own model.
- **Steady-state tenants cancel out.** CPU-only residents (embed/rerank)
  are deliberately not hibernated: they sit in both the baseline and the
  after reading, so their RAM/VRAM contribution subtracts away. Measuring
  a delta against a quiet box beats trying to account for every process.
- **A measurement is a config claim, so it stales like one.** The record
  is only used while it describes the present preset — same `CTX_SIZE` in
  the conf, same GPU pinning. Edit either and the scheduler falls back to
  the estimate until you re-measure, which beats silently trusting numbers
  taken under a different configuration.
- **First live run found two launch-path lies the fakes never told.** (1)
  serve.start *requests* a port, but the dispatcher's `--preset` file mode
  lets the `.conf` own the slot — and the materialized confs carried no
  `PORT`, so the measurement server quietly bound the `:8080` default while
  the ready-wait polled the requested `:8090` for 600 s. Materialized
  confs now carry the catalog's `PORT`/`VISIBLE_DEVICES` appended.
  (2) Some llama.cpp builds answer `/v1/models` in the legacy
  `{"models": […]}` shape, not OpenAI `{"data": […]}` — a readiness probe
  that only reads `data` never fires. Parse both. The restore path,
  meanwhile, worked exactly as designed: the failed measurement still
  brought brain and specialist back on its own.

Same week's tooling lesson, smaller but reusable: the scripts that operate
on the box — `slash-run.py` (fires a slash command at `/api/chat`, streams
the run, auto-approves the confirmation so a 30-minute `model.measure`
doesn't die on the 300 s confirm timeout), `eval-peek.py`, `eval-delta.sh`
— all **drive the live API, never the live checkout**. The ops surface is
HTTP; the git tree stays pristine, and the script works from anywhere that
holds the token.

---

### 3.22 Picking the brain, round two: flip sets and paired tests

§3.16's method kept running after its thirteen candidates, and the crown
moved twice more — cyber-tiel-35B-A3B, then occamy 1.0 (a Qwen3.6-35B-A3B
co-work SFT: post-trained for tool-call grounding, state tracking and
long-horizon recovery — the orchestrator job description as a training
objective). The occamy search (full table and failure analysis in
[docs/brain-bakeoff.md](docs/brain-bakeoff.md)) sharpened the method
itself:

- **The full set is noise on a close call; the flip set is the
  measurement.** Occamy's full-delta aggregate (74/93, 80%) sat on top of
  the incumbent's ~80% estimate — indistinguishable at these Wilson
  widths. The signal lived in the ten cases that actually move between
  brains, run 3 reps each: occamy 20/30 against cyber-tiel's ~18/30
  historical, and `graph-orientation` solved *without delegation* — a
  case cyber-tiel never passed. Pick the cases that distinguish, and
  spend the reps there.
- **Paired test or it didn't happen.** The two occamy builds — the
  abliterated MiniPlus quant vs official-weights Q4_K_M — finished 20/30
  vs 18/30. Net +2 at n=30, and a paired McNemar exact on the discordant
  reps is nowhere near significance: the verdict was written as a coin
  flip (pick by preference on abliteration vs official weights), not as a
  ranking. `scripts/eval-peek.py --compare A B` is the discipline —
  latest result per case, discordant pairs only. Same rule, same week, on
  the stall-ladder re-ablation: 81% vs 72% aggregate but 8-vs-3
  discordant, p=0.23 — recorded as "targeted recoveries confirmed,
  aggregate unproven at n=58", not as a win.
- **Judge notes carry grading provenance — read it.** When the primary
  judge returns unparseable content a fallback judge grades the run, and
  the note is tagged as such; one occamy-window note arrived that way. A
  single note is a lead, not a fact — a fallback-graded single note even
  less so. The sibling rail one level up: `eval.invalid_on_fallback`
  pulls a whole *case* out of the tally when a fallback model served any
  of its turns — the wrong brain's verdict is unmeasurable, so the row is
  kept for visibility and excluded from the numbers.

The model-level verdict repeated §3.16's architecture bet rather than
revising it: the new reference brain is another routing-tuned,
small-active MoE that delegates on its own (9 of 22 full-delta coding
cases, all well-aimed), holds the discipline cases (j-space-loop,
web-freshness), and fails where it fails on answer precision —
answer-guard territory, not brain territory. The spec-conflict trap stays
the honesty sentinel for brain swaps (1/3 — still brain-side).
Big-brain-hogs-the-wheel never came back; small router plus strong
specialist is the settled shape.

### 3.23 The audit loop: fix the instance, kill the shape

The October audits ran the same play five times, and the play is the
lesson: a finding isn't done when the bug is fixed — it's done when the
*shape* of the bug can't ship again.

- **The capability tier turns a found gap into a CI gate.** The instance:
  `procedure.save` bypassed every write gate — under dispatch mode the
  brain could write AND run its own code through save+run, the exact
  route `brain_mode: dispatch` exists to close. The class answer:
  lifecycle tools now declare `admin_only = True` on the class, enforced
  at selection, dispatch and slash even when no config pattern names
  them, and a completeness sweep (`tests/test_tool_tiers.py`) fails CI
  for any tool that spawns host processes or drives
  `ServeStart`/`ModelUse`/`JobStart` without the tier or a justified
  written exception. The next procedure.save-shaped gap dies at PR time,
  not in an audit.
- **Declared over config.** The tier moved the admin boundary from the
  `security.admin_only_tools` name list onto the tool classes themselves;
  the config list stays as the operator's way to restrict MORE, never
  less — the same direction the list itself already ran, merged over a
  code-side floor (`DEFAULT_ADMIN_ONLY_TOOLS`) so a missing or misspelled
  key can't silently switch the policy off. Policy that matters lives in
  code; config is for tightening.
- **Snapshot the tree, not HEAD.** The authored-check red→green baseline
  first used `HEAD` as the "before" state — but agents rarely commit
  between steps, so pre-existing uncommitted work got credited to the
  specialist, and a check green on both sides had never discriminated
  anything. The baseline is now a temp-index snapshot of the working tree
  at spawn time (`add -A` → `write-tree` → `commit-tree` on a throwaway
  index, the user's real index untouched), and a failed snapshot marks
  discrimination *unverifiable* instead of silently falling back to HEAD.
  A wrong baseline is worse than none.
- **A per-run predicate wants per-run picks.** The lean dispatch prompt
  was loaded once at boot while the dispatch gate re-evaluates per run —
  a live slot move to a non-coding preset (or a config flip in the
  editor) could split prompt from gate. Both prompts now load at init and
  `_system_prompt` picks per run with the gate's own predicate. When a
  decision is per-run, everything derived from it is per-run, or it
  drifts.
- **Skip-listening validators hide dead cases.** `validate_case_dict`
  moved from a 140-line hand-rolled validator to a pydantic model stack
  with the error wording remapped 1:1 — and the reconciliation surfaced a
  real bug: `EXPECT_KEYS` lacked `canary_not_in_tool_args`, so both
  shipped injection eval cases had been silently skipped at load, suite
  green the whole time. A loader that warns and skips turns a schema bug
  into missing coverage nobody misses. The cases load again (and passed
  their first live runs) — and the 1:1 remap is what made the swap safe
  enough to attempt in the first place.

### 3.24 Rationalization tables: quote the excuse, attach the rebuttal

Not every chronically skipped step earns a rail. For the ones that don't,
the skill layer gained a pattern (borrowed from addyosmani/agent-skills,
documented in the `writing-great-skills` skill, retrofitted to `tdd` and
`implement-from-spec` with excuses observed in real eval traces): put a
small table directly under the skipped step, each row quoting the excuse
the agent tells itself in first person — "I'll write the test after, it's
faster" — with the rebuttal attached. It works where "don't skip X"
doesn't: a prohibition is a negation and names the elephant, while
quoting the excuse catches the thought mid-formation — the model
recognizes its own line and the counter is already there. The discipline
keeps it honest: one table per step, at most five rows, only excuses
actually seen in traces (invented rows are sediment), delete a row that
stops firing — and the table supplements the step's completion criterion,
never replaces it. §3.15's hierarchy stands: this is the prompt-layer
tool for steps no mechanism covers yet, not a substitute for one.

### 3.25 Harness experiments: evidence before adoption

§3.13's eval loop measures a change after a human accepts it. **Harness
Experiments** move the comparison earlier without removing that gate. Admin →
Eval → Experiments replays one scripted case through four matched arms:
baseline, an invariant candidate (general agent discipline), a domain-specific
candidate, and a composed overlay. Prompt, skill, tool-description and selected
config changes exist only for those runs; live artifacts and the ordinary
results ledger stay untouched. The composer may reuse only targets it actually
probed, and only an explicit admin action turns the result into one atomic
brain-overlay proposal. Acceptance versions the whole composed edit together;
rollback appends an older snapshot rather than deleting history. One rep
explores; three are the minimum before believing a delta.

The model-written overlay is structured output, so it gets the same treatment
as a tool call (§1.2): parse it, validate its targets, and reject anything
outside the allowlist. Malformed JSON or a validation failure earns **one**
correction attempt containing the exact error; a second invalid response aborts
instead of burning an unbounded retry loop. Every attempt counts against the
shared cost cap. The lesson is not "let the harness rewrite itself" — it is
"let it propose controlled contrasts, keep the evidence, and make adoption a
separate human decision."

### 3.26 Caveman Compression: compact at generation time

Token economics (§3.2) also applies to text users never see: hidden reasoning,
delegation tasks and reports, and model-to-model calls. **Caveman Compression**
asks the model to write those scoped messages compactly while preserving facts,
inference steps, negations and uncertainty. This happens at generation time;
JayNet never deletes words from a completed answer. Post-hoc stripping could
remove the one "not" that changes the meaning.

The boundary matters more than the style. User-facing prose, code, JSON,
commands and verbatim evidence stay exact, and independent config switches
control each internal scope. A missing `caveman` section keeps old installs
off. Compression is therefore a prompt contract, not a lossy cleanup pass —
and its token savings remain a hypothesis to measure with matched A/B runs,
not a benefit to assume.

### 3.27 Radiance conversions: three occamy builds

Radiance is a separate C++/HIP inference engine, not another GGUF variant for
llama.cpp. Its `.rad` container carries the weights, tokenizer/chat template,
optional vision tower and speculative draft head. Building the engine is only
the first step:

```sh
scripts/build_tools.sh radiance rocm
```

We converted occamy 1.0 (Qwen3.6-35B-A3B) three ways. The comparison is useful
because each build answered a different question:

| container | weight plan | size | what we learned |
|---|---|---:|---|
| `occamy-1.0-fp8.rad` | block-FP8 RTN linears, 2-bit MTP draft | ~34.5 GiB | Fast and coherent, but one 32 GB card needed an 8 GiB host pool for the expert overflow. The RAM cost made it a poor fit for this box. |
| `occamy-1.0-w4.rad` | int4 RTN routed experts, FP8 trunk/shared expert | 19.65 GiB | Fully VRAM-resident with no host pool, but behavioral quality fell: **171/215 vs 187/215** for GGUF occamy. Not shippable as brain. |
| `occamy-1.0-w4g.rad` | same layout, but GPTQ-calibrated routed experts | 19.65 GiB | The calibrated candidate. JSON and vision canaries passed; the ship/rollback decision still comes from the same-case eval, never from the smoke test. |

**Conversion is model configuration, not just compression.** The dry run is a
required review gate:

```sh
rad-convert "$SRC" --add "$MTP" --recipe "$RECIPE" \
  --set quantization_config.quant_method=fp8 \
  --set quantization_config.fmt=e4m3 \
  --set mtp_num_hidden_layers=1 \
  --plan-only -v -o /dev/null
```

- The bf16 checkpoint had an empty quantization descriptor, so no architecture
  plugin claimed it. The two FP8 `--set` values are metadata overrides that
  select the `qwen35moe` plugin; they do not quantize the bf16 source themselves.
- The main checkpoint did not contain occamy's MTP head. `--add` merged the
  separately published tensors, but the source config said
  `mtp_num_hidden_layers: 0`; without changing that to 1, the added head was
  silently ignored.
- The plan caught logical-name mistakes such as `attn_out` vs `attn_output`.
  Unmatched weights quietly remain bf16, while a pattern matching the wrong
  tensor can damage quality. Review every large linear before spending hours
  on conversion.

**RTN and GPTQ fail differently.** RTN rounds weights independently. GPTQ uses
activation second moments (Gram matrices, the Hessian approximation) to
compensate for rounding error in the directions actual inputs exercise. The
RTN w4 build showed why this matters for an orchestrator: GAIA knowledge stayed
intact (38/48 for both RTN and GGUF), but irrelevance dropped to 13/30 vs
21/30 and compliance cases stopped emitting tool calls. A model can remember
everything and still lose its post-trained discipline.

The calibrated build used a corpus matched to that discipline: 294 chunks,
about 2M tokens, extracted from **local** JayNet traces, with every tenth chunk
held out and never used for calibration. A TP2 radiance tap served the RTN w4
container for 1M prefill tokens and wrote 20,640 Gram files. Conversion then
reused the unchanged trunk from the w4 container and recalculated only the
routed experts:

```text
rtn  codes=i4 group=128 scale=bf16 transform=fwht128
  → gptq codes=i4,group=128,scale=bf16,transform=fwht128,calib=$CALIB
```

Calibration needs GPU quiet time; the final conversion is host-only. Private
traces stay on the machine. Afterwards, `rad-info` must show `gptq calib=…`
under **by what made it** — a `w4g` filename alone proves nothing.

**Vision is a build-time capability too.** A vision tower in the container is
not enough if the radiance binary lacks media decoding. Installing the ffmpeg
development files and rebuilding with `RAD_WITH_FFMPEG=ON` enabled it. Check
`/v1/models` for `image: true` and make one real image request; the GPTQ smoke
correctly identified the objects in the test image.

Finally, **one artifact gets one preset name**. `occamy-1-0`,
`occamy-radiance` and `occamy-radiance-gptq` remain separate because eval rows
are keyed by the brain's preset name. Repointing the old preset would mix RTN
and GPTQ results and destroy the comparison. The radiance preset itself stays
small — select a radiance-typed binary and GPU, then:

```ini
MODEL_PATH=/srv/models/radconv/occamy-1.0-w4g.rad
CTX_SIZE=65536
KV_CACHE_DTYPE=fp8
MAX_NUM_SEQS=8
```

The FP8 build additionally needed `EXTRA_ARGS=--host-pool-mib 8192`; w4 did
not. Backend translation and vision capabilities are covered in
[docs/models.md](docs/models.md#backend-typed-presets). The broader lesson is
§3.22 again: conversion checks prove that the artifact is well formed, smoke
tests prove that it serves, and only paired behavioral evals prove that it is
still the brain you meant to run.

### 3.28 Recall on demand, compaction without evidence loss

Memory tools are useful only when retrieval happens at the right moment.
Prefetching every memory and document into every turn would tax the prompt and
invite irrelevant private context; waiting for a small model to choose
`memory.search` means natural questions often never retrieve anything. JayNet's
compromise is **intent-gated recall**: an explicit memory question can prefetch
a few owner-scoped hits before the first brain turn, while RAG additionally
requires a configured or explicitly named collection. The evidence is capped,
traced and privacy-tainted like an ordinary tool result. Storage requests do
not trigger reads, and child runs do not inherit the prefetch.

Retrieval quality still needs an abstention path. RAG's `min_score` is applied
**before** reranking: a stronger second stage may order good candidates, but it
must not resurrect evidence that already failed the relevance floor. In the
knowledge graph, aliases keep alternate names pointed at one canonical entity;
merging existing entities is explicit and confirmation-gated. These are small
mechanisms for the same principle: retrieval should add evidence, not confidence.

Compaction gets the complementary treatment. Replacing every old tool result
with a generic stub saves tokens but destroys exactly the error tail or command
needed for recovery. The evidence-aware pass therefore removes superseded file
slices, keeps both ends of execution logs plus their exit codes, protects
recent/pinned results and retains only the newest inline image. Nothing is
irrecoverable: the trace keeps the full result. The lesson is to compact by
**future usefulness**, not just size.

### 3.29 A fallback is a different experiment

Model routing needs the same provenance discipline as model conversion (§3.27).
If a specialist fails and LiteLLM serves the brain instead, the run may look
healthy while no longer testing the specialist at all. JayNet now records the
requested and served aliases, surfaces recent fallbacks in Admin → Status, and
marks affected eval rows invalid by default. Scoping that evidence to the eval
run tree matters: a concurrent chat's fallback must not poison an unrelated
case. Preflight catches a degraded source before a suite, and one bounded retry
absorbs a model that recovers mid-suite without creating a retry loop.

Capabilities are per target, too. A vision brain does not make a text-only
specialist multimodal, so child runs resolve `caps.vision` from the preset
actually serving their alias. Finally, restoration is another placement
operation, not a simple start: evict the temporary swap-in first, then restore
the original onto its measured hardware. The general lesson: **a successful
response proves neither which model answered nor that the box returned to its
intended state — record both.**

---

## 4. Links for more

Start here, in this order:

- **Anthropic — "Building effective agents"** — short, foundational blog post
  on agent design patterns.
- **`runtime/loop.py`** in this repo — the 200 lines everything above points
  at; [docs/architecture.md](docs/architecture.md) is the guided map.
- **llama.cpp `tools/server/README.md`** — the local inference stack's real
  documentation; [docs/llama-ops.md](docs/llama-ops.md) translates it.

When you want to go deeper:

- **ReAct paper** (Yao et al., 2022) — the reasoning-loop lineage you just
  implemented. **Toolformer** (Schick et al., 2023) — tool-augmented LMs
  formalized. **arXiv 2607.05391** — "LLM-as-a-Verifier", the idea behind
  `verify.score`.
- **Karpathy — "Let's build GPT"** — if you want to see inside the box
  you're orchestrating.
- **MCP spec** (modelcontextprotocol.io) — the protocol JayNet's MCP bridge
  speaks. **Qdrant tutorials** — when RAG is your next step.
- **LiteLLM docs** — proxy routing, fallbacks, caching, in more depth than
  JayNet uses.
- **RLM paper** (arXiv 2512.24601) — the context-as-variable pattern
  §3.10 implements natively.
- **J-space cognition** — the research behind `skills/j-space`; the
  skill's own `references/` directory carries a readable science digest.
- **Open-Jev** (zefan-cai.github.io/open-jev) — the open decision-model
  implementation §3.18 tested; its docs are a careful example of publishing
  *measured limits* alongside scores.

### Cheat sheet

| Term | Meaning |
|---|---|
| Agent loop | call model → run its tool calls → feed results back, until answer or limit |
| Tool call / result | model-emitted JSON asking for a function; the executed outcome appended as `role:"tool"` |
| System prompt | first `role:"system"` message; sets behavior and constraints |
| Token | ~¾ of an English word; the unit of context and cost |
| Context window | max tokens per call; KV cache lives here |
| Quantization (Q4_K_M, IQ4_XS, …) | lossy weight compression trading quality for VRAM/speed |
| GGUF | the file format llama.cpp loads |
| MoE | Mixture-of-Experts: many params total, few active per token (fast decode) |
| Prompt caching | unchanged prefix tokens billed at a fraction on repeat calls |
| RAG | retrieve relevant document chunks, inject into the prompt |
| Trace | persistent per-step log of a run, for replay and debugging |
| Loop guard | refusal of the same tool call repeated 3× with no write between — degenerate-loop tripwire |
| Guard / rail | a registered check the loop applies at a fixed phase (pre-turn, post-tool, final-answer); a mechanism, not a prompt wish |
| guard_fired | uniform event every guard application emits (`{"name","phase","turn"}`) — the rails' fire-rate telemetry |
| Bounce cap (`max_bounces_per_answer`) | a final answer bounces at most N times (default 3, 0 = off), then is accepted with a `bounce_cap` event |
| Repeat hard block (`hard_block_repeat_errors`) | the 4th identical (tool, args, error) failure refused at dispatch (default 3, 0 = off) |
| admin_only_tools | admin-grade tools hidden from selection AND refused at dispatch for non-admin accounts |
| guards_off | benchmark-variant knob disabling named guards — the ablation that tells you which rails pay |
| eval-peek --compare | paired McNemar exact test between two brain labels; single-rep deltas inside the Wilson band aren't results |
| Stall ladder | escalating "act now → delegate → produce or ask" injections after consecutive no-progress turns; only work-product changes reset it |
| Dispatch mode | `tools.code.brain_mode: dispatch` — the brain's own source-file writes are rejected pre-exec; coding routes to the specialist, period |
| Verify-the-delegate bounce | a final answer delivering delegated implementation with no check tool run after it bounces once — delegation ≠ verified |
| Taint | marker on a conversation that saw private data; blocks cloud calls until you opt in |
| Bi-encoder / cross-encoder | fast independent embedding scorer vs precise joint reranker — the two RAG stages |
| K-quant | GGUF quantization family with mixed per-tensor precision (`Q4_K_M` = default sweet spot) |
| Compaction | lossy summary of older history when the window fills; exact state belongs in files |
| kg.* vs graph.* | the agent's typed facts about your world vs a static-analysis map of a codebase — unrelated, despite the shared word |
| Subcall | budgeted, traced one-shot model call inside a run; the RLM primitive |
| RLM | "context as a variable": bulk stays in files, subcalls map over slices, the model reduces |
| Chain | declarative pipeline with fixed steps; choreography instead of a re-decided loop |
| Plugin | optional extension bundle (tools + hooks + routes + skills), toggled in Admin → Harness → Plugins |
| Eval harness | flagged sessions → regression cases → judge proposals → measured fixes |
| Procedure | a skill with a task-shape tag + checklist; auto-loaded on match, its steps enforced by the loop's stall/final-answer checks |
| Decision model | typed probabilities over fixed choices in one forward pass (Jev-type) — classification without generation |
| Capability tier (`admin_only`) | a tool class's own declaration that it's admin-grade — enforced at selection, dispatch and slash even when config never names it; the config list can only restrict more |
| Flip set | the ~10 cases that actually move between brains, run ×3 reps — where a brain comparison carries signal; full-set aggregates drown it in Wilson overlap |
| Rescuer / doom-rail | guard telemetry reading: pass-rate-when-fired high = the rail rescues runs; ~0 = it only ever sees doomed ones — the `guards_off` candidate list |
| Fallback judge | grades a run when the primary judge returns unparseable content; the note is tagged — a single fallback-graded note is a lead, not a fact |
| Rationalization table | skill pattern: under a chronically skipped step, quote the agent's excuse first-person and attach the rebuttal — only excuses seen in real traces |
| Harness experiment | matched baseline/candidate arms with run-scoped overlays; evidence first, proposals only by explicit admin action |
| Brain overlay | append-only per-preset snapshots of accepted lessons; active-slot scoped, hot-applied, reversible without deleting history |
| Caveman Compression | scoped generation-time compact style for internal reasoning and handoffs; exact artifacts and user-facing prose are never post-processed |

---

## Part 2 — how we got here

History and rationale: the measured lessons behind Part 1's
mechanisms. The sections keep their original §3.x numbers —
references elsewhere (docs, the playbook, code comments)
point at them.

### 3.15 Three decisions worth stealing

- **Freeze the toolset for a run.** Loading tool schemas mid-run changes
  the prompt prefix and busts the cache (§3.2) — decide the tools once and
  keep them stable.
- **Split the gate from the decision.** The loop decides *whether* a call
  needs approval; a pluggable provider decides *how* to ask (web card,
  auto-deny, CLI prompt). A declined call returns as a visible error, so
  the model adapts instead of hanging.
- **Adopt standards late.** JayNet deferred MCP until concrete external
  servers earned it: for tools you write yourself, a bridge adds a hop and
  sits outside your budget/privacy machinery. Native first, protocol when
  it pays.
- **Enforce in the loop, don't beg in the prompt.** Prompts persuade and
  models eventually ignore them; mechanisms don't. We measured the whole
  ladder on one behavior (getting the brain to delegate coding to the
  specialist): a standing prompt bullet — ignored for months; a mid-run
  nudge riding the tool result — the brain wrote 4× past it; the rule
  appended to the *tool description* the model reads at the decision point —
  better, still advisory; a pre-exec *rejection* ("inline implementation is
  closed — delegate first") — delegation went 0/5 → 4/5 on the eval delta
  the same day. And the final rung, added when small brains were measured
  retrying blocked calls past 10+ rejections: the loop guard **runs the
  delegation itself** (`auto_delegate_after` — harness-picked route,
  de-anchored request, 10/12 conversions in clean conditions). Each rung is
  thresholded and has an off-ramp (single-model
  installs stay untouched, any actual delegation disarms the gate). If a
  behavior matters, spend a mechanism on it — and expect to climb rungs.

---

### 3.16 Benchmarking the brain: what thirteen candidates taught us

The orchestrator brain is the harness's multiplier, and intuition is a bad
selector for it — ours said "the biggest MoE you can fit". Because JayNet
has an eval library that runs cases through the *real* loop, we could stop
guessing: thirteen brain candidates (plus harness variants on the champion)
ran the same hard-tail delta suite
(`scripts/eval-delta.sh` — stable 3×-pass cases skipped, 10% re-included as
regression sentinels, so the set is biased hard by construction), and every
result landed in one comparable table, [docs/brain-bakeoff.md](docs/brain-bakeoff.md).
The candidates, in order: Ornith 1.5 35B-A3B MoE, K2-Horizon-MoVA 36B-A4B
MoE, Ling-3.0-tiny (7.9B/A1.3B), Gemma-4 19B-A4B, K2-Horizon-7B dense,
Spark-X2.5-4B MoE (paired with a 27B coding specialist split across both
GPUs), NeoHorse-1 9B dense (a general-reasoning RL tune), ZDTaichu-5.0 9B,
Spark-4B Q8 @f16-KV/261k (the 52/89 champion config), MiMo-V2.6-Distill 9B,
Spark-X2.5-1.7B, Ternary-Bonsai-2-27B (a 1-bit-era ternary 27B at ~9 GB
VRAM, vision included), and qwen35-9B (the 80 t/s speed record).

What the table taught us:

- **Sub-5B-active brains can't hold standing instructions under load.**
  Ask-back discipline, skill loading, output format — all regress when the
  context fills. Below that mass, no prompt tuning rescues it — and the
  qwen35 column showed the same pattern can reach *up* into 9B when the
  tune isn't instruction-first: two empty answers, a 900-call runaway
  loop, fabricated-from-memory "research".
- **Delegation count is a better brain-health metric than pass rate.**
  The tripwire works when the model is *willing* (Gemma: two of its six
  passes only happened because it handed work to the specialist) and is
  ignored when it isn't (Ling: zero voluntary delegations and two context
  blowups). The auto-delegate column made the same point from the other
  side: when the harness forces the hand-off, the blind-spot cases
  convert — a brain that won't route makes the whole specialist
  architecture decorative, so route for it.
- **A small obedient brain + a strong specialist beats a big brain that
  hogs the wheel.** The 7B dense outscored every larger candidate (50% on
  the hard tail, all five discipline cases green, five voluntary
  delegations) — and its failures were honest capability misses, not
  discipline failures. Capability you can patch with a specialist;
  discipline you can't. The champion refines this further: a **4B
  MoE** brain built for agentic routing (Spark) plus a tensor-split 27B
  coding specialist — 52/89 with auto-delegate at ~3× the speed.
- **Speed is a brain feature only while the pass column holds.** qwen35
  ran 80 t/s — 2.5× the Bonsai brain — and still lost 26 vs 32 on the
  identical set: fast and wrong is just wrong sooner. Its wins were real
  though (first-ever pass on the spec-conflict trap, where every other
  brain caved to the wrong test) — raw capability was never the question;
  standing-instruction discipline was.
- **Quantization formats buy different things.** The ternary Bonsai
  (PQ2_0) runs a 27B at ~9 GB VRAM with working vision — that footprint
  is what lets a 27B brain and a 27B specialist share two GPUs with 262k
  contexts. On RDNA4 it buys *VRAM, not speed* (~32 t/s vs the dense 7B's
  60+): pick ternary when the constraint is memory, dense when it's
  latency.
- **Harness hardening moves behavior that weights don't.** Same brain,
  new rails: capping reasoning per turn (thinking is completion tokens,
  §3.2 — uncapped, a brain burned the whole cap on thinking and then
  looped on `todos` instead of acting), the stall ladder above, and the
  delegate gate together flipped cases without touching a single weight.
  Budget an evening for rails before you budget a download for a bigger
  model.
- **Gates force the route, not the follow-through.** Dispatcher mode made
  every brain delegate; the new failure was delivering the specialist's
  work unchecked (a twice-delegated regex that matched 1/9 sample dates).
  Hence two more rails: the verify-the-delegate bounce (delegated
  implementation + no check tool after it = the final answer bounces
  once) and the fresh-context delegation review (a *stronger* model
  judges the report against its evidence — never the brain that ordered
  the work). Watch *where* the failure moves after each gate; that's the
  design loop.
- **Tune beats size.** NeoHorse, a 9B dense general-reasoning RL tune,
  scored *below* the 4B agentic-tuned MoE (34% vs 56%) despite double the
  parameters and flawless gate compliance — its failures were verification
  and discipline, exactly what the agentic tune trains. Pick the model
  trained for the job the harness needs done.
- **Tool errors are part of the interface.** Small models send `todos`
  updates in the wrong JSON shape and address items by title, not id; an
  error that just says "have ids [1,2,3]" burns turn after turn because
  the model can't map its intent to the valid ids. Tolerate the common
  wrong shapes, and make every error carry enough state to self-correct in
  one call — "no todos yet — create them first" converted a stall-guard
  loop into a recovery. An error message is a UI — for a
  reader that can't ask questions.
- **Variance is real.** Single-run pass/fail wobbles; flaky cases sit near
  50% for every candidate. Compare columns, not cells.

The method is the reusable part: pick the cases your harness *fails*, run
every candidate against exactly that set, and let the table — not the
parameter count — pick the brain. The search didn't stop at thirteen: the
crown moved twice more (cyber-tiel-35B-A3B, then occamy 1.0), and the
method grew sharper rules of its own — §3.22 is the current round.

---

### 3.20 Show the budget: scarcity beats advice

The cheapest harness fix we ever measured was one line of text. Our eval
flakes had a pattern: the brain would verify a date it already knew five
times over, or search fifteen iterations before asking the user — then get
failed by the judge for exceeding an iteration cap **it had never seen**.
The cap existed only as an invisible hard stop at the end. The fix was not
a rule ("don't over-verify" — advice like that lives in the prompt already
and gets ignored); it was **visibility**: a `budget: iteration 3/8` line
re-injected at the prompt tail every turn, rebuilt per turn, never stored
in the transcript. Overnight the over-verification and over-search flakes
disappeared (5/5 on the targeted set). The 3× repeat then separated two
things the 5/5 had blurred: *pacing* was fixed everywhere, but *protocol
compliance* was not — j-space runs did all the work right and still
skipped the mandatory `run.badge` 3/3. Advice and visibility don't enforce
protocol; a gate does. So once the j-space skill loads, file edits stay
rejected at dispatch until the badge is set — and when the first gated
runs showed the brain badging and then dodging the *planning* step by
delegating the edits instead, the gate grew to cover both lanes: edits
AND delegation stay blocked until badge + a todos plan exist
(`loop_guard.jspace_badge_gate`). One rejection teaches what ten prompt
rules didn't — and each gate teaches you where the next dodge lives.
Delegated sub-agents get their own readout with their own budget, since
they run the same loop path.

Pair that with the counter-experiment from the same week: the CLM
state-file (§3.11's self-managed `state.md`, tail-anchored the same way)
got a real A/B — 8 cases × 3 reps per arm — and showed **no pass-rate
benefit for +30% tokens and ~2× wall time**, so it stays off (numbers in
`docs/clm-bakeoff.md`). Together they're the honest version of "context
engineering": tail-anchored injections are nearly free to build, some pay
(budget visibility), some don't (model-maintained state), and the only
way to tell them apart is an enforced budget, a fixed case list, and a
driver script that runs both arms — folklore says add more context, the
ablation says prove it.

---

*Theory companion for JayNet v1.41.x. Operations live in [docs/](docs/);
the product story in [README.md](README.md).*
