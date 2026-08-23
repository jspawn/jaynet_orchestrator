# Learning Guide — how LLM agents actually work

> A short theory companion to JayNet. It explains the ideas behind the
> code so that a curious newcomer can read the codebase afterwards, and an
> experienced reader can skip straight to the parts they don't know yet.
> Setup and operation live in [docs/](docs/); this file is about
> understanding. The code is the source of truth — this is the map.

Already know what an agent loop is? Skim [§3](#3-short-deep-dive) and
[§4](#4-links-for-more); the rest is groundwork.

---

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
`trace.db` and shown in Admin → Status → Recent runs (and `scripts/orch
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
own hardware. Admin → Presets shows the catalog.

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

### 3.8 Compaction: when the window fills

Contexts fill, and two mechanisms answer that at two scales. *Inside a
run*, oversized tool results get stubbed — the loop replaces old bulk with
a placeholder plus a path, because the model rarely needs the full bytes
twice. *Across a chat*, `/compact` summarizes the older history into a
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
slice it with `code.execute`, map budgeted one-shot **subcalls** over the
slices, reduce the partial answers yourself. The model sees pointers and
distillates, not bulk. In JayNet this is native, not a plugin:
`context.stage` + workspace files + `code.execute` + subcalls, with the
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
in Admin → Plugins, default-off, no core changes. Toggles apply live:
enable registers the plugin into the running process, disable removes
exactly what it added, so iterating on a plugin is a toggle cycle rather
than a restart (fresh pip dependencies excepted); a plugin may even ship
its own admin UI. Graphify is the
reference implementation and sets the bar: it ships staleness semantics
(the graph is a snapshot, not a truth) and a skill that teaches when to
query it. Benchlab is the second — benchmark harnesses (Terminal-Bench,
GAIA) packaged the same way — and its existence matters more than its
content: two plugins prove the interface right in a way one never can.
The design lesson mirrors the MCP note below: adopt structure when a
second real consumer earns it.

### 3.13 The supervised self-improvement loop

The eval harness closes a loop most agent projects leave open. A flagged
or stuck session becomes a regression *case* with one click; a suite run
replays cases through the real agent loop — not a mock — against the live
harness; a judge model turns failures into concrete *proposals* (prompt
edits, skill rewrites, tool-description fixes, config changes) that a
human applies or dismisses; the next suite run measures the effect.
Statistics track pass rates, flakiness and trends; the benchmark tab
shoots candidate models against each other on the same suite before you
swap brains. Two disciplines keep it trustworthy: proposals are
suggestions under human review, never self-applied, and benchmark
repetitions are labeled so they don't pollute the trend numbers. The
theory: an agent you can't regression-test is a demo.

### 3.14 Three decisions worth stealing

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
| Taint | marker on a conversation that saw private data; blocks cloud calls until you opt in |
| Bi-encoder / cross-encoder | fast independent embedding scorer vs precise joint reranker — the two RAG stages |
| K-quant | GGUF quantization family with mixed per-tensor precision (`Q4_K_M` = default sweet spot) |
| Compaction | lossy summary of older history when the window fills; exact state belongs in files |
| kg.* vs graph.* | the agent's typed facts about your world vs a static-analysis map of a codebase — unrelated, despite the shared word |
| Subcall | budgeted, traced one-shot model call inside a run; the RLM primitive |
| RLM | "context as a variable": bulk stays in files, subcalls map over slices, the model reduces |
| Chain | declarative pipeline with fixed steps; choreography instead of a re-decided loop |
| Plugin | optional extension bundle (tools + hooks + routes + skills), toggled in Admin → Plugins |
| Eval harness | flagged sessions → regression cases → judge proposals → measured fixes |

---

*Theory companion for JayNet v1.2.x. Operations live in [docs/](docs/);
the product story in [README.md](README.md).*
