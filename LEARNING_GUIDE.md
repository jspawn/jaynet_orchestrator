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
`config/runtime.yaml`. Hit one and the run ends loudly with
`budget_exceeded`, it never runs away. This is the discipline that makes
Level 1 safe to own.

**The privacy gate → taint.** Tools in private namespaces (files, RAG,
notes) *taint* the conversation. While tainted, calls to cloud LLM tools are
refused unless you explicitly opt in (`share_private`). Coarse, but actually
enforced — at the dispatcher, where the model can't paraphrase around it.
Try it: index a private note, then ask the brain to have a cloud model
summarize it without opting in, and read the refusal in the trace.

**Local brain, cloud as tools.** The orchestrator is a small local model;
cloud models (Claude, Gemini, …) exist only as `llm.*` tools it may call.
The cloud never sees your conversation — each call gets a self-contained
task description. Routing is cheap and free locally; only real work costs
money. That's the whole local-first idea in one pattern.

**Presets → models as managed infrastructure.** The brain can list model
presets and load one mid-chat (`model.use`) — a coding specialist for a hard
patch, then back. On smaller hardware this is the key trick: one swappable
slot serves many finetuned experts, because only the one the current task
needs is loaded. Models are not fixed endpoints; the agent reconfigures its
own hardware. Admin → Presets shows the catalog.

**Studio → the agent helps build its own extensions.** In Admin → Studio an
admin drafts skills (versioned know-how the brain loads on demand), chains,
declarative API connectors and Python tools — with AI-assisted drafting by
the local model, validated before save, shareable as `.jaypack`. The theory
bit: skills and tools are just more structured context the loop can use
([docs/studio.md](docs/studio.md)).

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
│    a. loop guard — same call twice already? refuse           │
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

**Prompt caching breaks the compounding.** Providers bill unchanged prefix
tokens (system prompt + tool schemas, stable across turns) at ~10%. The
LiteLLM proxy in front of everything makes this a free ~10× on input cost.

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

---

*Theory companion for JayNet v0.9.x. Operations live in [docs/](docs/);
the product story in [README.md](README.md).*
