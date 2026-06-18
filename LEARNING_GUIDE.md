# Building a Local LLM Orchestrator — A Learning Guide

> A companion document for the orchestrator project in this repository. Read
> alongside the code; the code is the source of truth, this is the map.
>
> **This edition is also a build-yourself manual.** Most sections end with a
> **Build it yourself** block covering the file(s) that section explains: a
> short spec of what to write and why, the gotchas, and a collapsible
> *Reference implementation* with the complete file. Follow the blocks in order
> and you can reconstruct the entire `phase 1–4` tarball from scratch — the
> theory section teaches the idea, the build block makes you implement it.
>
> **Phases 5–14 are built and documented across §§12–23** — the capability tools
> (§12), RAG and managing it from the web (§13), the confirmation gate and event
> sink (§14), the FastAPI + SSE web console (§15), file uploads and delivery (§16),
> access control with TOTP two-factor (§17), a self-test harness the agent can
> drive (§18), sub-agents (§19), runtime-loadable skills (§20), serving models on the GPUs
> (§21), a field guide to
> the bugs the tests caught (§22), and what's still ahead (§23). These are a
> design-and-decisions map, not full reference listings — the repo is the source
> of truth.
>
> Conventions:
> - **Spec first, code second.** Try to write each file from the spec before
>   expanding the reference. The understanding is in the attempt.
> - File paths are relative to `/srv/orchestrator/` unless noted.
> - The reference listings are the exact contents of the shipped tarball.

---

## Table of contents

1. [What you're building](#1-what-youre-building)
2. [Theory: how LLM agents actually work](#2-theory-how-llm-agents-actually-work)
3. [Architectural decisions and trade-offs](#3-architectural-decisions-and-trade-offs)
4. [Real-world examples](#4-real-world-examples)
5. [Setup, step by step](#5-setup-step-by-step)
6. [Usage and observability](#6-usage-and-observability)
7. [How the code works (a guided tour)](#7-how-the-code-works-a-guided-tour)
8. [Operating llama.cpp](#8-operating-llamacpp)
9. [Quantization: how it works and how to choose](#9-quantization-how-it-works-and-how-to-choose)
10. [Embeddings and rerankers (the RAG stack)](#10-embeddings-and-rerankers-the-rag-stack)
11. [Exercises to deepen understanding](#11-exercises-to-deepen-understanding)
12. [The capability tools](#12-the-capability-tools)
13. [RAG: retrieval and management](#13-rag-retrieval-and-management)
14. [The confirmation gate and the event sink](#14-the-confirmation-gate-and-the-event-sink)
15. [The web console](#15-the-web-console)
16. [Files in and out: uploads and delivery](#16-files-in-and-out-uploads-and-delivery)
17. [Access control and two-factor auth](#17-access-control-and-two-factor-auth)
18. [The self-test harness (`test.run`)](#18-the-self-test-harness-testrun)
19. [Sub-agents (`agent.spawn`)](#19-sub-agents-agentspawn)
20. [Runtime-loadable skills (`skill.load`)](#20-runtime-loadable-skills-skillload)
21. [Serving models on the GPUs (`serve.*`)](#21-serving-models-on-the-gpus-serve)
22. [Bugs the tests caught](#22-bugs-the-tests-caught)
23. [Still ahead](#23-still-ahead)
24. [Glossary](#24-glossary)
25. [Further reading](#25-further-reading)

---

## 1. What you're building

A **multi-LLM agent runtime** that runs entirely on your workstation, with a
small local model acting as the brain and cloud models (Claude, Gemini, GPT)
available as tools it can call when needed. The system is:

- **Local-first**: the orchestrator and its reasoning loop run on your hardware
  (dual R9700, Arch Linux). Cloud LLMs are *optional tools*, not the substrate.
- **Bounded**: every request has hard ceilings on iterations, wall-clock time,
  cost, and token usage. The system fails loudly when limits are hit rather
  than running away.
- **Plugin-based**: tools are dropped into a directory and auto-discovered.
  No central registration code, no recompilation. Add a tool, restart.
- **Observable**: every step is logged to SQLite. You can replay any run.
- **Privacy-aware**: tools have namespaces, some marked "private". Private
  results cannot leak into calls to cloud LLMs without an explicit opt-in.

### Why build this yourself?

You could just use LangChain, LlamaIndex, AutoGen, or any of a dozen frameworks.
The reason to build it yourself: **agent loops are about 200 lines of code, and
understanding those 200 lines is worth far more than a thousand lines of
framework abstractions you don't control.** Once you've built one, you can read
any framework's source and immediately recognize what each piece does.

This project is sized for that learning: small enough to hold in your head,
real enough to be useful, structured so each phase adds one concept.

---

**Build it yourself — the project skeleton, `README.md`, and package stubs**

*Goal:* lay down the directory tree, the empty Python packages that make the
plugin auto-discovery work, and a README that states the intent. Everything
else in this guide drops into this skeleton.

*What to create:*

1. The directory tree. The `tools/<namespace>/` folders are created now so the
   layout is stable and the registry has somewhere to look. Phases 1–4 use
   `llm`, `web`, `code`; phases 5–9 (§12) fill in `rag`, `fs`, `git`, `gpu`,
   `job`, `memory`, `kg`, `arxiv`, `eval` (plus the `mcp` stub) and add a `web/`
   package for the console.

   ```bash
   sudo mkdir -p /srv/orchestrator && sudo chown "$USER" /srv/orchestrator
   cd /srv/orchestrator
   mkdir -p config runtime prompts data systemd scripts agents web/static \
            tools/llm tools/web tools/code tools/rag tools/fs tools/mcp \
            tools/job tools/git tools/gpu tools/memory tools/kg \
            tools/arxiv tools/eval
   ```

2. **Empty `__init__.py` files** in `runtime/`, `tools/`, and every
   `tools/<namespace>/` directory. These are zero-byte on purpose — they only
   need to exist so Python treats the folders as importable packages. Without
   `tools/__init__.py` and `tools/<ns>/__init__.py`, the dotted import
   `tools.web.search_fetch` that the registry relies on (Section 7, Stop 2)
   won't resolve.

   ```bash
   touch runtime/__init__.py tools/__init__.py web/__init__.py \
         tools/llm/__init__.py tools/web/__init__.py tools/code/__init__.py \
         tools/rag/__init__.py tools/fs/__init__.py tools/mcp/__init__.py \
         tools/job/__init__.py tools/git/__init__.py tools/gpu/__init__.py \
         tools/memory/__init__.py tools/kg/__init__.py \
         tools/arxiv/__init__.py tools/eval/__init__.py
   ```

*Watch out for:* the `tool_base.py` and tool modules use `_`-prefixed helper
files are skipped by discovery, but `__init__.py` is skipped too (it starts
with `_`). That's why packages stay empty and real tools live in their own
named files.

<details>
<summary>Reference — directory tree + <code>README.md</code></summary>

```
/srv/orchestrator/
├── config/             # litellm.yaml, runtime.yaml, qwen3-tools.jinja
├── runtime/            # agent loop, budget, trace, registry, tool base
│   ├── events.py       # EventBus — transport-neutral run events (§14)
│   └── confirm.py      # confirmation providers, incl. web approval (§14)
├── tools/              # plugin tools, auto-discovered
│   ├── llm/  web/  code/   # phase 1–4 starters
│   ├── job/            # detached GPU job runner (§12)
│   ├── fs/             # filesystem read/search/edit (§12)
│   ├── git/            # repo inspect + checkpoint (§12)
│   ├── gpu/            # rocm-smi telemetry (§12)
│   ├── memory/  kg/    # persistent notes + knowledge graph (§12)
│   ├── arxiv/  eval/   # paper search + multi-model compare (§12)
│   ├── rag/            # local retrieval, SQLite+numpy (§13)
│   └── mcp/            # (stub — deliberately deferred, §12)
├── web/                # FastAPI + SSE console (§15)
│   ├── server.py       # endpoints + chat-history API
│   ├── store.py        # saved-chat SQLite store (§15)
│   └── static/index.html
├── agents/             # sub-agent YAML defs (phase 7, still ahead)
├── prompts/            # system prompts
├── data/               # trace.db, memory.db, rag.db, chats.db, jobs/
├── systemd/            # user units (incl. orchestrator-web.service)
└── scripts/            # CLI + launchers
```

`README.md`:

````markdown
# Local LLM Orchestrator

A privacy-first multi-LLM agent runtime for a dual-R9700 Arch Linux workstation.
This is a learning project — the goal is to understand how agent loops, tool
plugins, budgets, RAG, and MCP fit together by building each piece yourself.

## Architecture

- **llama.cpp server** (`:8090`) — local orchestrator brain on dual R9700 (ROCm). Port 8090 keeps clear of `serve.sh` (8080) and `sd-serve.sh` (8081).
- **LiteLLM proxy** (`:4000`) — unified OpenAI-compatible API for local + cloud LLMs.
- **Orchestrator runtime** — bounded agent loop with plugin tools, budgets, tracing.

This phase (1–4) gives you a working Level-1 agent:
- Reasoning loop with tool calls
- Three starter tool categories: cloud LLMs, web search/fetch, sandboxed code
- Budget enforcement (iterations, wall-clock, cost, tokens)
- SQLite trace logging
- Privacy gating between tool namespaces

Phases 5–8 land next: RAG, MCP bridge, filesystem tools, sub-agent loop.

## Learning path

The code is intentionally readable rather than maximally clever. A few entry
points if you want to poke at things:

- `runtime/loop.py` — the agent loop itself. Roughly 200 lines. Read this first.
- `runtime/tool_base.py` — the `Tool` contract. Adding a new tool is a single file.
- `runtime/budget.py` — how cost/iteration ceilings are enforced.
- `tools/llm/cloud_models.py` — pattern for wrapping any HTTP-backed model as a tool.
- `tools/web/search_fetch.py` — a real tool with parsing, fallbacks, error handling.

Try writing your own tool next — e.g. a `time.now` tool, a `math.eval` tool, or
a `notes.append` tool — and watch the registry auto-pick it up on restart.
````

(The shipped README also embeds the Setup and Shell-aliases sections; those are
built in Section 5 of this guide, so they aren't duplicated here.)

</details>

---

## 2. Theory: how LLM agents actually work

### 2.1. The completion API is stateless

Every interaction with an LLM is a single function call: text in, text out.
The model has no memory between calls. When you have a "conversation" with
ChatGPT, the client sends the *entire history* with every message.

```
POST /v1/chat/completions
{
  "model": "...",
  "messages": [
    {"role": "system",    "content": "You are helpful."},
    {"role": "user",      "content": "What's the capital of France?"},
    {"role": "assistant", "content": "Paris."},
    {"role": "user",      "content": "And of Germany?"}
  ]
}
```

This statelessness is the single most important fact for understanding agents.
An agent isn't a process that "thinks" — it's a *loop that reconstructs context
and re-asks the model* on every turn. The "memory" of the agent is whatever
your loop chooses to include in `messages` next time.

### 2.2. Tool use is just structured output

When you give a model "tools," what's actually happening:

1. You include tool *schemas* (JSON Schema definitions) in the request.
2. The model is trained to recognize when calling a tool would help, and to
   respond with a structured payload like `{"name": "web.search", "arguments": {"query": "..."}}`.
3. **You** — the orchestrator — execute that tool, get a result, and feed it
   back to the model as another `messages` entry with `role: "tool"`.
4. The model continues, potentially calling more tools or producing the final answer.

The model never "executes" anything. It generates JSON describing what it would
like to happen. Your loop decides whether to actually do it.

This is the core insight: **agentic behavior is in the loop, not the model.**
The model is a sophisticated pattern-matcher that's been trained to produce
useful tool-call JSON when appropriate. The agency lives in your code's
decision to call the model again with the tool result.

### 2.3. The four levels of agentic behavior

A taxonomy I find useful — these are roughly ordered by complexity and risk:

| Level | What it does | Examples |
|-------|-------------|----------|
| 0 | Single-turn tool use. Model calls tools once, then answers. | A weather app that asks the model to extract a city name, then queries an API |
| 1 | Bounded reasoning loop. Model can iterate: tool → result → tool → result → answer. | This project; most "function-calling" demos |
| 2 | Sub-agents / delegation. Specialized agents can be spawned for sub-tasks. | Claude Code, Cursor's composer, AutoGen |
| 3 | Long-running / autonomous. Persistent state, external triggers, hours-long runs. | Devin, Cowork, scheduled research agents |

Each level adds capability but also blast radius. Level 1 is the sweet spot for
most things — predictable cost ceiling, debuggable, still genuinely useful.

We build Level 1 now. Phase 7 sketches Level 2. Level 3 stays off the table
until there's a specific use case demanding it; the failure modes are too
expensive to debug speculatively.

### 2.4. Anatomy of a single agent loop iteration

```
┌────────────────────────────────────────────────────────────────┐
│ 1. Check budget (iterations++, time, cost, tokens)             │
│    → BudgetExceeded? Terminate gracefully.                     │
├────────────────────────────────────────────────────────────────┤
│ 2. Call the orchestrator model with current messages           │
│    POST /v1/chat/completions                                   │
│    → response.message has either:                              │
│        a) tool_calls = [...]    → step 3                       │
│        b) content (no tools)    → final answer, exit loop      │
├────────────────────────────────────────────────────────────────┤
│ 3. For each tool_call in response:                             │
│    a. Loop guard: same call twice already? Return error result │
│    b. Privacy gate: remote LLM + tainted convo? Reject         │
│    c. Look up tool in registry                                 │
│    d. Execute: result = await tool.execute(args, ctx)          │
│    e. Update budget with any LLM usage tool incurred           │
│    f. Append result as a message with role="tool"              │
│    g. If tool was private, taint the message index             │
├────────────────────────────────────────────────────────────────┤
│ 4. Loop back to step 1                                         │
└────────────────────────────────────────────────────────────────┘
```

All the interesting decisions live in step 3. The model picks *what* to do;
the loop decides *whether to allow it and how to surface the result.*

### 2.5. Token economics

Tokens are the unit of cost and context. Two important properties:

**Tokens compound across the loop.** Every iteration sends the entire message
history again. A 10-iteration loop where each turn adds 2k tokens of context
sends roughly `1 × 2k + 2 × 2k + 3 × 2k + ... + 10 × 2k = 110k tokens` of input
to the model in total, not 20k. Costs grow quadratically with loop length unless
you compress.

**Prompt caching breaks the compounding.** Anthropic, OpenAI, and Google all
offer some form of cached input: tokens that haven't changed since the last
request cost ~10% of full input. If your system prompt + tool schemas are
stable (and they almost always are), you can pay the full price once and the
discounted price for every subsequent turn. This is why LiteLLM's caching is
configured in this project — it's a free 10x improvement on input costs.

The other compounding cost is **tool result size.** If a `web.fetch` returns
50k tokens of HTML and the model needs to call it three times, that's 150k
tokens of result you'll be sending back on every subsequent turn. The
orchestrator should pre-summarize large results, and tools should truncate
defensively. You'll see this pattern throughout `tools/web/search_fetch.py`.

### 2.6. Why local models are good orchestrators

A common mistake is thinking the orchestrator needs to be the smartest model.
It doesn't. The orchestrator's job is:

1. Read the user request.
2. Decide which tool(s) to call.
3. Read tool results.
4. Decide what to do next.
5. Eventually produce a final answer.

Steps 1–4 are **routing**, which a 30B local model handles fine — especially
modern ones like Qwen3 that are explicitly trained for tool-calling. The
*heavy lifting* (drafting prose, deep reasoning, code generation) is delegated
to cloud models *as tools*. The orchestrator only sees their outputs.

This split has three big benefits:

- **Cost**: orchestration calls are free (local). Only the actual work calls hit cloud APIs.
- **Privacy**: only the explicit task description goes to the cloud, not the conversation history or other tool results.
- **Latency**: local first-token is fast; the slow cloud calls happen only when needed.

The trade-off is orchestration quality — local models are slightly worse at
recognizing when a tool would help. In practice, with a clear system prompt
and decent tool descriptions, the gap closes fast.

---

**Build it yourself — `prompts/orchestrator.md`**

*Goal:* write the system prompt that turns a general instruction-tuned model
into an orchestrator. This is the one place where 2.6's theory ("the agency is
in the loop, the policy is in the prompt") becomes a concrete artifact. The
loop loads this file verbatim as `messages[0]` on every run.

*What to write — the policy the prompt must encode:*

1. **Identity and job.** One paragraph: you are a local orchestrator; your job
   is to reason about the request and use tools when they help.
2. **Tool-economy rules.** Tell the model *not* to call tools when it already
   knows the answer. This is the single most cost-relevant line in the file.
3. **A model-selection ladder.** Map task types to the cheapest adequate model
   alias on the `llm.call` tool: `haiku`/`gemini_flash`/`qwen_flash` for
   classification and extraction; `claude` (Sonnet) for reasoning, careful
   writing, and code; `opus`/`qwen_max` only when genuinely needed; `gemini_pro`
   for long-context synthesis. This mirrors the cost table in `runtime.yaml` so
   the model's instinct lines up with what you're actually billed.
4. **Minimal-context rule.** Never forward the whole conversation to a remote
   LLM; build a self-contained task. (This is *advisory* here and *enforced* by
   `cloud_models.py`, which only sends `task` + `payload`.)
5. **Privacy note.** Results from `rag.*` and `fs.*` are private; passing them
   to remote LLMs will error without explicit permission. (Again, advisory in
   the prompt, enforced in `loop.py`.)
6. **Sequencing, stopping, honesty.** One call at a time when steps depend on
   each other; stop when done; admit limits instead of hallucinating.

*Watch out for:* keep it short. A 200-line system prompt eats context on every
single iteration (remember 2.5 — it compounds). The shipped version is ~40
lines. Also: every behavior you assert here that *matters* should be backed by
real enforcement in code. Treat the prompt as a hint and the dispatcher as the
law; the prompt reduces how often the model fights the law.

<details>
<summary>Reference implementation — <code>prompts/orchestrator.md</code></summary>

```markdown
# Orchestrator system prompt

You are a local orchestrator running on a private workstation. Your job is to
help the user by reasoning about their request and using the available tools.

## Core principles

1. **Use tools when they help, not for their own sake.** If you already know the
   answer and the user wants a quick reply, just answer. Tools are for fresh
   information, computation, or capabilities you don't have.

2. **Use the right LLM for the job.** Delegate subtasks with the `llm.call`
   tool, choosing its `model` argument by cost/capability. Default to the
   cheapest tier that can do the job; only climb to a frontier model when needed.

   - **Cheap / fast (classification, extraction, short rewrites, bulk work):**
     `model="haiku"`, `"gemini_flash"`, or `"qwen_flash"`. Reach here first.
     (These default to thinking OFF for speed.)
   - **General reasoning & careful writing (the workhorse tier):**
     `model="claude"` (Sonnet) is the default. `"qwen_plus"` is a strong, much
     cheaper alternative — good for second opinions or cost-sensitive runs.
   - **Hardest reasoning (use sparingly, expensive/slow):** `model="opus"` or
     `"qwen_max"`. Only when the workhorse tier has fallen short or the stakes
     justify it. Pass `think=true` for difficult multi-step reasoning.
   - **Code (generation, refactoring, debugging):** `model="qwen_coder"` is the
     specialist; `model="claude"` remains excellent for code that needs
     reasoning about a larger context.
   - **Long-context / multi-document synthesis:** `model="gemini_pro"` (very
     large context) or `"qwen_max"`.
   - **A different model's perspective / tiebreaker:** when two answers disagree
     or you want a cross-check, call `llm.call` again with a model from a
     different provider (e.g. `"qwen_plus"` vs `"claude"`).

   Rules of thumb: prefer the cheap tier and only escalate on evidence; the
   `think` argument forces reasoning on/off when you need to override the
   per-model default; for non-English tasks the Qwen models are often both
   cheaper and stronger; don't call three models when one will do — every call costs tokens and time.

3. **Pass minimal context to remote LLMs.** Never forward the entire conversation.
   Build a self-contained task description with only what's needed.

4. **Privacy matters.** Results from `rag.*` and `fs.*` tools are considered
   private to this workstation. You will get an error if you try to pass them
   to remote LLM tools without explicit user permission.

5. **One tool call at a time when results depend on each other.** Parallel calls
   are fine for independent operations (e.g., web.search + rag.search), but if
   step 2 needs the output of step 1, do them sequentially.

6. **Stop when done.** When you have the answer, write it in plain text. Do not
   call more tools "just to be thorough."

7. **Be honest about limits.** If a tool fails, the budget is nearly exhausted,
   or you genuinely don't know, say so. Don't hallucinate.

## Tool call format

Use the OpenAI tool-call format provided by the runtime. Each call gets a result
back; you may then make further calls or produce a final answer.
```

</details>

---

## 3. Architectural decisions and trade-offs

Every system design is a series of choices. Here's why this one is shaped the
way it is, and what the alternatives would have looked like.

### 3.1. Why LiteLLM in the middle?

**Decision**: All LLM calls go through a LiteLLM proxy at `127.0.0.1:4000`,
which speaks OpenAI's API format and translates to each provider.

**Alternative**: call each provider's SDK directly from the dispatcher tools.

**Why LiteLLM wins**:
- One auth point, one place to rotate keys.
- Unified spend tracking across providers (the orchestrator records this in its own `data/trace.db`).
- Built-in retry, fallback, and response caching.
- The local llama.cpp server is *also* OpenAI-compatible, so it slots in
  alongside cloud providers with zero special-casing.

**The cost**: an extra hop (~1ms localhost). Negligible.

**Build it yourself — `config/litellm.yaml`**

*Goal:* configure the proxy that 3.1 argues for. It exposes one
OpenAI-compatible endpoint on `:4000` that fronts your local llama.cpp server
*and* every cloud provider, with retries, fallbacks, and caching. (Cost is tracked by the orchestrator’s own trace.db, so the proxy runs stateless — no DB.)

*What to write:*

1. **`model_list`** — one entry per logical model name your tools reference.
   The local model points at llama.cpp's `:8090/v1` with a dummy key; each cloud
   entry maps a friendly name (`claude-sonnet`) to a provider model string and
   reads its key from the environment via `os.environ/VAR`. The friendly names
   here must match the `model=` strings in `cloud_models.py` and the keys in the
   `costs:` table in `runtime.yaml`.
2. **`router_settings`** — retries, timeout, and a `fallbacks` map so a failing
   premium model degrades to a cheaper one (and ultimately to the free local
   model) instead of erroring the whole run.
3. **`litellm_settings`** — turn on response caching (this is the free 10×
   from 2.5 made real), and `drop_params: true` so provider-specific params
   that one backend doesn't understand are silently dropped rather than 400ing.
4. **`general_settings`** — `master_key` from env (the `LITELLM_MASTER_KEY`
   every component authenticates with). No `database_url` — the proxy runs
   stateless (cost lives in the orchestrator's `trace.db`); adding one would make
   LiteLLM try to init Prisma at startup and crash unless `prisma` is installed.
   Plus `ui_access_mode: admin_only`.

*Watch out for:* the local model's `model:` must be prefixed `openai/` so
LiteLLM treats llama.cpp as an OpenAI-compatible backend. Keep the friendly
names consistent across all three files — a typo here surfaces as a confusing
"model not found" three layers up in the loop.

*On the roster:* the reference below wires up Claude, Gemini, and Qwen (the set
in use). Two things to keep in sync as you add or drop a provider: (1) every
`model_name` needs a matching row in `runtime.yaml`'s `costs:` table or the
budget tracker silently bills $0 for it, and (2) each provider needs its key in
`~/.config/orchestrator.env` (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`DASHSCOPE_API_KEY`). An entry for a provider you have no key for is harmless
until the model is actually called, at which point it errors — so a config can
list more providers than you have keys for. Exposing a model here is only half
the job: a *tool* must also exist for the orchestrator to call it (see §7 Stop 6
/ the consolidated `llm.call` in §3.7), and the model-selection guidance in the
system prompt (§2) should mention it. To add a provider — e.g. OpenAI, Mistral,
DeepSeek — add a `model_name` block with that provider's LiteLLM prefix
(`openai/`, `mistral/`, `deepseek/`, …), a `costs:` row, the env key, and a tool
alias.

⚠️ Provider prefixes and model-id strings drift fast. The values below were
verified against provider docs in late May 2026; confirm each against the
current catalog before relying on it. (3.5 Pro wasn't GA yet, so Gemini Pro
points at 3.1 Pro; Qwen 3.7 Max was preview-only, so Qwen Max uses the stable
`qwen3-max`.)

<details>
<summary>Reference implementation — <code>config/litellm.yaml</code></summary>

```yaml
# LiteLLM proxy config — unified OpenAI-compatible API on :4000
# Backends: local llama.cpp + Claude + Gemini + Qwen.
#
# ⚠️  Model ids and provider prefixes verified against provider docs in
#     late May 2026. They drift fast — confirm each `model:` against the
#     provider's current catalog before relying on it. Add more providers
#     later by adding a model_name block + a costs: row in runtime.yaml + an
#     env key; remove one by deleting its block (and any fallback that names it).
#
# Provider prefixes: anthropic/  gemini/  dashscope/ (Qwen)
#                    openai/ + api_base  ->  local llama.cpp

model_list:
  # ===================== LOCAL ORCHESTRATOR =====================
  - model_name: local-orchestrator
    litellm_params:
      model: openai/qwen3-30b-a3b
      api_base: http://127.0.0.1:8090/v1
      api_key: "not-needed"
      max_tokens: 32768

  # ===================== ANTHROPIC (Claude) =====================
  - model_name: claude-opus
    litellm_params:
      model: anthropic/claude-opus-4-8
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: claude-sonnet
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: claude-haiku
    litellm_params:
      model: anthropic/claude-haiku-4-5
      api_key: os.environ/ANTHROPIC_API_KEY

  # ===================== GOOGLE (Gemini) =====================
  # 3.5 Pro not GA as of late May 2026; 3.1 Pro is the current Pro tier.
  # gemini-3.5-flash is GA and the current default Gemini model.
  - model_name: gemini-pro
    litellm_params:
      model: gemini/gemini-3.1-pro
      api_key: os.environ/GEMINI_API_KEY

  - model_name: gemini-flash
    litellm_params:
      model: gemini/gemini-3.5-flash
      api_key: os.environ/GEMINI_API_KEY

  # ===================== QWEN (Alibaba DashScope) =====================
  # OpenAI-compatible. International endpoint shown; for the Beijing region
  # swap api_base to https://dashscope.aliyuncs.com/compatible-mode/v1
  # (qwen3.7-max is preview/aggregator-only right now — using stable GA ids.)
  - model_name: qwen-max
    litellm_params:
      model: dashscope/qwen3-max
      api_key: os.environ/DASHSCOPE_API_KEY
      api_base: https://dashscope-intl.aliyuncs.com/compatible-mode/v1

  - model_name: qwen-plus
    litellm_params:
      model: dashscope/qwen3.6-plus
      api_key: os.environ/DASHSCOPE_API_KEY
      api_base: https://dashscope-intl.aliyuncs.com/compatible-mode/v1

  - model_name: qwen-flash
    litellm_params:
      model: dashscope/qwen3.5-flash
      api_key: os.environ/DASHSCOPE_API_KEY
      api_base: https://dashscope-intl.aliyuncs.com/compatible-mode/v1

  - model_name: qwen-coder
    litellm_params:
      model: dashscope/qwen3-coder-plus
      api_key: os.environ/DASHSCOPE_API_KEY
      api_base: https://dashscope-intl.aliyuncs.com/compatible-mode/v1

router_settings:
  routing_strategy: simple-shuffle
  num_retries: 2
  timeout: 120
  # If a model errors/rate-limits, fall through these. Chains end at the free
  # local model so a run degrades gracefully instead of hard-failing.
  fallbacks:
    - claude-opus:   ["claude-sonnet", "claude-haiku"]
    - claude-sonnet: ["claude-haiku", "qwen-plus", "local-orchestrator"]
    - gemini-pro:    ["gemini-flash", "local-orchestrator"]
    - qwen-max:      ["qwen-plus", "local-orchestrator"]
    - qwen-plus:     ["qwen-flash", "local-orchestrator"]
    - qwen-coder:    ["qwen-plus", "local-orchestrator"]

litellm_settings:
  cache: true
  cache_params:
    type: local
    ttl: 600
  set_verbose: false
  drop_params: true
  request_timeout: 120

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  # No database_url: runs the proxy stateless (no Prisma/DB needed). Cost/usage
  # is tracked by the orchestrator's own trace.db (runtime/trace.py), so
  # LiteLLM's spend DB would be redundant. Adding a database_url here makes
  # LiteLLM try to init Prisma at startup and crash unless prisma is installed.
  ui_access_mode: "admin_only"
```

</details>

### 3.2. Why a plugin registry instead of a giant `tools.py`?

**Decision**: Tools live in `tools/<namespace>/<verb>.py` and are auto-discovered.

**Alternative**: import all tools in `runtime/loop.py` and pass them explicitly.

**Why plugins win**:
- Adding a tool is a single file. No edits to the loop.
- Tools group naturally by namespace (`web.*`, `code.*`, `llm.*`).
- Per-agent allowlists become trivial: pass `allowed=["web.search", "code.execute"]`
  to `registry.openai_schemas()`.
- Plugin systems scale; explicit registration doesn't.

**The cost**: import-time errors are silent (logged but skipped). Mitigation:
`registry.discover()` logs every registration, and the smoke test in section 5
will tell you if a tool failed to load.

### 3.3. Why bounded loops with hard budgets?

**Decision**: Every request gets `max_iterations`, `max_wall_clock_s`,
`max_cost_usd`, `max_total_tokens`. Hitting any one terminates the run.

**Alternative**: let the model decide when to stop.

**Why bounds win**: a misconfigured prompt or a stubborn model can burn $50
of Opus tokens in seconds. Bounds are cheap to add, save you from your own
bugs, and force you to think about what "done" means. The system fails loudly
rather than silently overspending.

**The cost**: occasionally a legitimate task gets cut off. The CLI's
`--max-cost`, `--max-iterations` flags let you raise ceilings deliberately
when you actually need to.

### 3.4. Why privacy by namespace, not per-result?

**Decision**: Tools in the `rag` or `fs` namespace are marked private; their
results cannot be passed to the remote LLM tool (`llm.call`).

**Alternative**: tag individual pieces of data as sensitive.

**Why namespace-level wins**: data-level tagging is theatre. The model can
paraphrase, summarize, or transform "tagged" data into "untagged" data with
ease. The only reliable enforcement is **at the dispatcher**: if your
conversation has touched any private-namespace tool, the dispatcher refuses
remote LLM calls unless `share_private=true`. Coarse, but actually enforced.

**The cost**: occasionally you want to send a small piece of private data to
Claude, and have to either opt in for the whole conversation or restructure.

### 3.5. Why Vulkan instead of ROCm for the local model?

**Decision**: build llama.cpp with `-DGGML_VULKAN=ON`, not ROCm/HIP.

**Alternative**: ROCm 7.x with the HIP backend.

**Why Vulkan wins on R9700 right now** (May 2026):
- AMD's RDNA4 ROCm support is officially there but uneven — FP8 needs patches,
  tensor parallel has open bugs, AITER doesn't recognize gfx1201.
- Mesa's RADV Vulkan driver is mature, very well-tested (every Linux gamer uses it).
- Recent llama.cpp benchmarks show Vulkan matching or beating ROCm on RDNA4
  for typical LLM workloads.
- Vulkan path doesn't depend on the ROCm userspace stack at all.

**The cost**: you lose access to some experimental ROCm-only optimizations.
For inference workloads on a single user's box, this doesn't matter.

### 3.6. Why Qwen3-30B-A3B as the orchestrator?

**Decision**: Qwen3-30B-A3B-Instruct (MoE, ~3B active params) as the default.

**Alternatives considered**:
- Qwen3-32B dense: stronger reasoning, ~3x slower decode.
- Llama 3.3 70B: still stronger, fits in 64GB at Q4, but overkill for routing.
- Smaller models (7-13B): not reliable enough at tool-call format.

**Why Qwen3-30B-A3B wins**: native tool-call training (the Jinja template ships
with the model card), MoE means decoding is fast because only 3B params activate
per token, and the model is small enough to leave headroom for KV cache and
optional second models on GPU 1.

### 3.7. Tool-loading strategy: all tools vs. on-demand

**Decision**: send *every* tool's schema on *every* model turn (the base loop
calls `registry.openai_schemas()` with no allowlist). Optionally, trim the set
**once per run** via a `ToolSelector`.

**Alternative**: load tools dynamically *per turn* — show the model only the
tools a given step seems to need.

This is the "save tokens by loading tools only when needed" idea, and it has a
trap worth understanding before reaching for it.

**The numbers** (approximate). The orchestrator *now ships* the consolidated
`llm.call` (one tool, model enum — see Stop 6), but the comparison that motivated
it is still the clearest way to see the cost of tool schemas:

| Set | ~tokens of schema |
|-----|------------------:|
| Hypothetical 7 separate `llm.call_*` tools | ~1,200 |
| **Consolidated `llm.call` (shipped)** | **~220** |
| `web.*` (2) | ~240 |
| `code.*` (1) | ~130 |

Seven near-identical per-model tools would have been ~77% of the schema;
collapsing them to one `llm.call` with a `model` enum cut that to ~220 tokens —
a flat saving on *every* run, no routing logic, no cache downside.

**Why per-turn dynamic loading usually backfires.** Tool schemas are a *stable
prefix*: they sit at the front of the prompt and don't change between turns.
That's exactly what prompt caching (the "free 10×" from §2.5;
`cache: true` in `litellm.yaml`) is built to exploit — after the first turn the
whole toolset already costs ~10%. If you *change* the toolset mid-run, you
invalidate the cached prefix and pay full price again on the turn it changes.
With the toolset already this small, the cache you'd be busting is worth more
than the schema you'd be trimming. Naive per-turn loading can *increase* cost.

**So the rule is: decide once, freeze for the run.** Select the toolset before
the loop starts and hold it constant. The cache stays warm, and you still drop
the schemas a run genuinely won't use. That's what the `ToolSelector` below does,
with three modes:

- **`all`** (default) — expose everything. Zero behaviour change; maximally
  cache-stable. This is the shipped default.
- **`static`** — expose only a caller-provided allowlist (`--tools web,code`).
  Deterministic; you know the run only needs certain tools.
- **`auto`** — a cheap, deterministic heuristic: always expose a configured
  *core* set (`llm` by default — delegation is the orchestrator's main job),
  plus any namespace whose trigger keywords appear in the user message. No extra
  LLM call, so the decision is reproducible and itself free.

An explicit `--tools` list always wins over the mode. Selection happens once in
`run()`, before the `while True`, and the frozen `tools_schema` is reused every
iteration — so cache stability is preserved by construction.

**The bigger lever — already taken: schema consolidation.** Collapsing the
per-model tools into a single `llm.call` with a `model` enum is the shipped
design (Stop 6). It's a bigger win than selective loading and it scales: adding a
provider is one enum value, not a new `Tool` subclass — better in both tokens and
in maintenance. The trade-off: the model picks the model via an enum argument
instead of by choosing a tool, so the per-value `description` has to carry the
cost/capability guidance (keep it aligned with the system-prompt tiers in §2).
Sketch, for the current Claude + Gemini + Qwen set:

```python
class CallCloudLLM(Tool):
    name = "llm.call"
    description = "Delegate a self-contained task to a cloud model. Pick `model` by cost/capability."
    parameters = {
        "type": "object",
        "properties": {
            "model": {"type": "string",
                      "enum": ["haiku", "gemini_flash", "qwen_flash",   # cheap/fast
                               "claude", "qwen_plus",                    # workhorse
                               "opus", "qwen_max",                       # frontier
                               "qwen_coder",                             # code
                               "gemini_pro"],                            # long context
                      "description": (
                          "cheap/fast: haiku, gemini_flash, qwen_flash. workhorse "
                          "reasoning/writing: claude, qwen_plus. frontier (costly): "
                          "opus, qwen_max. code: qwen_coder. long-context: gemini_pro. "
                          "Default to the cheapest tier that can do the job.")},
            "task": {"type": "string"}, "payload": {"type": "string"},
            "system": {"type": "string"},
            "format": {"type": "string", "enum": ["text", "json"]},
        },
        "required": ["model", "task"],
    }
    # enum value -> litellm.yaml model_name
    _MAP = {
        "haiku": "claude-haiku", "claude": "claude-sonnet", "opus": "claude-opus",
        "gemini_flash": "gemini-flash", "gemini_pro": "gemini-pro",
        "qwen_max": "qwen-max", "qwen_plus": "qwen-plus",
        "qwen_flash": "qwen-flash", "qwen_coder": "qwen-coder",
    }

    async def execute(self, args, ctx):
        model = self._MAP.get(args["model"])
        if model is None:
            return ToolResult(status="error", result=None,
                              error=f"unknown model alias: {args['model']}")
        return await _call_via_litellm(model, args["task"], args.get("payload"),
                                       args.get("system"),
                                       args.get("format") == "json", ctx)
```

Adding a provider later is one enum value + one `_MAP` line + a `litellm.yaml`
block + a `costs:` row. If you adopt this tool, the system prompt's "Use the
right LLM for the job" section should describe the `model` enum values rather
than separate `llm.call_*` tools.

**The cost of selecting:** in `static`/`auto` modes a query that needs a tool the
heuristic didn't expose will simply fail to use it (the model can't call what it
can't see) and should say so. Keep `auto` generous, and re-run with
`--tools …` or leave `mode: all` when in doubt. This is why the default is `all`.

**When to use which:** stay on `all` until token cost actually bites (it usually
doesn't, thanks to caching). Reach for `static` when you're scripting batch runs
whose tool needs are known. Reach for `auto` once you have many namespaces and
most queries touch only a couple. Reach for consolidation first, always — it's
free.

**Build it yourself — `runtime/selector.py` (+ loop, CLI, and config wiring)**

*Goal:* add cache-safe, per-run tool selection. This is an **optional
optimization layered on the base system** — wire it in after your base loop
(§7, Stop 5) runs, since it patches that file.

*What to write:*

1. **`runtime/selector.py`** — a `ToolSelector(registry, config)` whose
   `select(user_message, requested)` returns an allowlist of tool names or
   `None` (meaning "all"). `requested` (the `--tools` list) always wins; else
   dispatch on `mode`. `_expand()` turns caller items into names, supporting
   namespace shorthands (`web` → every `web.*`) and dropping unknowns with a
   warning. `_auto()` unions the `core_namespaces` with any namespace whose
   keyword substring-matches the (lower-cased) message. Return results in
   registry order; fall back to `None` if selection is empty so the model is
   never stranded toolless.
2. **Patch `runtime/loop.py`** (three edits): import `ToolSelector`; build
   `self.selector` in `__init__`; add a `tools: list[str] | None = None` kwarg
   to `run()` and replace the schema line with a *once-before-the-loop*
   selection (plus a trace event so `--tools` choices show up in `--trace`).
3. **Patch `scripts/orch`**: add a `--tools` option, split it on commas, pass
   `tools=…` into `run()`.
4. **Add the `tool_selection` block to `config/runtime.yaml`** (shown in the
   §3 config build block below).

*Watch out for:* select **once**, outside the `while True`. If you ever move
selection inside the loop you reintroduce the cache-busting this whole section
exists to avoid. And keep the default `mode: all` so existing behaviour is
untouched until someone opts in.

<details>
<summary>Reference implementation — <code>runtime/selector.py</code></summary>

```python
"""Per-run tool selection.

Decide ONCE per request which tools to expose to the orchestrator, then freeze
that set for the whole run. Freezing is the point: the tool schemas are a stable
prefix, so a fixed set stays prompt-cache-friendly. Changing the toolset
mid-run would bust the prefix cache and usually costs more than it saves.

Modes (config: tool_selection.mode):
  all     - expose everything (default; zero behaviour change)
  static  - expose only the caller-provided allowlist (--tools); else all
  auto    - deterministic keyword->namespace heuristic on the user message,
            plus a configured always-on core set. No extra LLM call, so the
            decision is cheap, reproducible, and cache-stable within a run.

An explicit caller allowlist (e.g. `--tools web,code`) always wins, regardless
of mode. Namespace shorthands expand: `web` -> every `web.*` tool.
"""

from __future__ import annotations

import logging

from .registry import ToolRegistry

log = logging.getLogger(__name__)


class ToolSelector:
    def __init__(self, registry: ToolRegistry, config: dict):
        self.registry = registry
        sel = config.get("tool_selection") or {}
        self.mode: str = sel.get("mode", "all")
        # Namespaces always exposed in `auto` mode. `llm` by default because
        # delegating to cloud models is the orchestrator's primary job —
        # hiding it would cripple the model more than it would save.
        self.core: set[str] = set(sel.get("core_namespaces", ["llm"]))
        # {namespace: [keyword, ...]} — if any keyword appears in the user
        # message, that namespace's tools are added (auto mode).
        self.keywords: dict[str, list[str]] = sel.get("keyword_namespaces", {})
        # Optional hard cap on number of tools exposed (None = unlimited).
        self.max_tools: int | None = sel.get("max_tools")

    def select(self, user_message: str, requested: list[str] | None = None) -> list[str] | None:
        """Return a frozen allowlist of tool names, or None meaning 'all tools'.

        Called once, before the loop starts. The result is held constant for the
        whole run so the tool-schema prefix never changes mid-conversation.
        """
        names = [t.name for t in self.registry.all()]

        if requested:                       # explicit allowlist always wins
            allow = self._expand(requested, names)
            chosen_via = "static"
        elif self.mode == "auto":
            allow = self._auto(user_message, names)
            chosen_via = "auto"
        else:                               # "all" or "static" without a list
            return None

        # Preserve registry order, drop anything unknown, apply optional cap.
        ordered = [n for n in names if n in allow]
        if self.max_tools:
            ordered = ordered[: self.max_tools]

        log.info("Tool selection (%s): %d/%d tools -> %s",
                 chosen_via, len(ordered), len(names), ordered)
        # Empty selection would leave the model with no tools at all; fall back
        # to 'all' rather than strand it.
        return ordered or None

    # ---------- internals ----------

    def _expand(self, requested: list[str], names: list[str]) -> set[str]:
        """Expand a caller list of exact names and/or namespace shorthands."""
        allow: set[str] = set()
        valid_ns = {n.split(".", 1)[0] for n in names}
        for raw in requested:
            item = raw.strip()
            if not item:
                continue
            if item in names:                       # exact tool name
                allow.add(item)
            elif item in valid_ns:                  # namespace shorthand
                allow.update(n for n in names if n.split(".", 1)[0] == item)
            else:
                log.warning("Requested tool/namespace not found: %s", item)
        return allow

    def _auto(self, user_message: str, names: list[str]) -> set[str]:
        """Deterministic heuristic: core namespaces + keyword-triggered ones."""
        msg = (user_message or "").lower()
        allow = {n for n in names if n.split(".", 1)[0] in self.core}
        for ns, kws in self.keywords.items():
            if any(kw.lower() in msg for kw in kws):
                allow.update(n for n in names if n.split(".", 1)[0] == ns)
        return allow
```

</details>

<details>
<summary>Patch — <code>runtime/loop.py</code> (3 edits)</summary>

```python
# 1. add to the imports near the top
from .selector import ToolSelector

# 2. at the end of AgentRuntime.__init__
        self.selector = ToolSelector(self.registry, self.config)

# 3a. widen the run() signature
    async def run(self, user_message: str, *, share_private: bool = False,
                  budget_overrides: dict | None = None,
                  tools: list[str] | None = None) -> dict:

# 3b. replace:   tools_schema = self.registry.openai_schemas()
#     with a once-per-run, frozen selection + a trace event:
        allowed = self.selector.select(user_message, requested=tools)
        tools_schema = self.registry.openai_schemas(allowed)
        self.trace.log(run_id, "tool_selection", 0, {
            "mode": self.selector.mode,
            "requested": tools,
            "selected": allowed if allowed is not None else "all",
            "count": len(tools_schema),
        })
```

</details>

<details>
<summary>Patch — <code>scripts/orch</code> (add <code>--tools</code>)</summary>

```python
# add the option (alongside the other @click.option lines)
@click.option("--tools", "tools_csv",
              help="Comma-separated tool/namespace allowlist, e.g. 'web,code.execute'. "
                   "Overrides config tool_selection for this run.")

# add tools_csv to the cli() signature, then before runtime.run(...):
    tools = [t.strip() for t in tools_csv.split(",")] if tools_csv else None
    tools = [t for t in tools if t] if tools else None
    result = asyncio.run(runtime.run(
        message,
        share_private=share_private,
        budget_overrides=overrides or None,
        tools=tools,
    ))
```

</details>

Usage once wired in:

```bash
orch --tools code "What is 7 factorial?"          # expose only code.*
orch --tools web,llm "Latest ROCm version?"        # web + delegation
# or set mode: auto in runtime.yaml and let the heuristic decide per run
```

---

**Build it yourself — `config/runtime.yaml`**

*Goal:* the single config file that encodes every decision from this section.
The loop reads it once at startup and threads it through every tool via
`ToolContext.config`. If you understand this file, you understand the system's
knobs.

*What to write — one block per decision in Section 3:*

1. **`orchestrator`** — the local model name (must match a `model_name` in
   `litellm.yaml`), the LiteLLM base URL, and the path to the system prompt
   you wrote in Section 2.
2. **`budgets`** — the four ceilings from 3.3: `max_iterations`,
   `max_wall_clock_s`, `max_cost_usd`, `max_total_tokens`. These are defaults;
   the CLI overrides them per request.
3. **`privacy`** — from 3.4: `private_tool_namespaces: [rag, fs]` (which
   namespaces taint the conversation) and `remote_llm_tools: [...]` (which tools
   the taint blocks). The loop reads both to enforce the gate.
4. **`trace`** — DB path and `log_content` (full content vs metadata-only,
   per Stop 4 in Section 7).
5. **`tools`** — per-namespace settings: the web search endpoint and caps, the
   code sandbox type, timeout, and workdir.
6. **`costs`** — USD per 1M tokens for every model name, used by
   `budget.add_usage()` to convert tokens to dollars. Local model is `{input:
   0, output: 0}` because it's free. Keep these names in lockstep with
   `litellm.yaml` and the cost arguments in `cloud_models.py`.

*Watch out for:* this file is the contract between three components. The model
names appear here (`costs`), in `litellm.yaml` (`model_list`), and in
`cloud_models.py` (the `_call_via_litellm("claude-sonnet", ...)` calls). A
mismatch means either a crash or — worse — silent zero-cost accounting.

<details>
<summary>Reference implementation — <code>config/runtime.yaml</code></summary>

```yaml
# Orchestrator runtime config

orchestrator:
  # The model that drives the reasoning loop. Reaches LiteLLM at :4000.
  model: local-orchestrator
  litellm_base: http://127.0.0.1:4000
  # System prompt file (relative to /srv/orchestrator/)
  system_prompt: prompts/orchestrator.md

# Default budgets per request. Caller can override via CLI/API.
budgets:
  max_iterations: 10
  max_wall_clock_s: 300
  max_cost_usd: 1.00
  max_total_tokens: 200000

# Tool exposure strategy. Decided ONCE per request and frozen for the run, so
# the tool-schema prefix stays prompt-cache-stable (see guide §3.7).
#   all    - expose every tool every turn (default; zero behaviour change)
#   static - expose only the caller's --tools allowlist (else all)
#   auto   - core_namespaces + any namespace whose keyword appears in the message
tool_selection:
  mode: all
  core_namespaces: [llm]        # always exposed in `auto` (delegation is the core job)
  keyword_namespaces:
    web:  [search, google, latest, current, news, url, http, website, "look up", recent, today, price, weather, "who is", "what is"]
    code: [calculate, compute, math, sum, average, regex, parse, json, convert, factorial, fibonacci, "how many"]
  max_tools: null               # optional hard cap on exposed tools (null = unlimited)

# Privacy enforcement. Tools marked `private: true` produce results
# that the dispatcher refuses to forward to non-local LLM tools
# unless `share_private: true` is set on the request.
privacy:
  default_share_private: false
  # Tools whose results are considered "private" by default. The orchestrator
  # may NOT use these results as arguments to non-local LLM tools.
  private_tool_namespaces: [rag, fs]
  # LLM tools that count as "remote" for privacy purposes.
  remote_llm_tools: [llm.call]

# Tracing
trace:
  db_path: /srv/orchestrator/data/trace.db
  enabled: true
  # Log full tool args/results (true) or just metadata (false)
  log_content: true

# Tool-specific config
tools:
  web:
    # Use a self-hosted SearxNG if you run one. Falls back to DuckDuckGo HTML.
    search_endpoint: null   # e.g. http://127.0.0.1:8888/search
    fetch_timeout_s: 15
    max_content_chars: 50000

  code:
    # Sandbox via firejail. Set to null to disable sandboxing (NOT recommended).
    sandbox: firejail
    timeout_s: 30
    allowed_imports: [math, statistics, json, re, datetime, decimal, random, hashlib]
    workdir: /srv/orchestrator/data/code-sandbox

# Rough cost table for budget tracking (USD per 1M tokens).
# ⚠️ PLACEHOLDER PRICING — verify against each provider's pricing page. Every
# model_name in litellm.yaml needs a row here or budget.add_usage() silently
# bills $0 for it. Local model is free.
costs:
  local-orchestrator: {input: 0,    output: 0}
  # Anthropic
  claude-opus:        {input: 15,   output: 75}
  claude-sonnet:      {input: 3,    output: 15}
  claude-haiku:       {input: 1,    output: 5}
  # Google Gemini
  gemini-pro:         {input: 2,    output: 12}
  gemini-flash:       {input: 0.50, output: 3}
  # Qwen (Alibaba DashScope)
  qwen-max:           {input: 2.50, output: 7.50}
  qwen-plus:          {input: 0.40, output: 1.20}
  qwen-flash:         {input: 0.05, output: 0.40}
  qwen-coder:         {input: 1,    output: 5}
```

</details>

---

## 4. Real-world examples

Once you have this running, here's the spectrum of things it can do — from
trivial to non-trivial — with annotations on which tools fire.

### Example 1: Simple computation

```
$ orch "What's the 17th Fibonacci number?"
```

Expected loop:
- Iteration 1: model calls `code.execute` with a Fibonacci snippet.
- Iteration 2: model sees the result, writes the final answer.

Total: ~2 iterations, ~1k tokens, $0.00 (all local).

### Example 2: Current information

```
$ orch "What's the latest stable ROCm version, and which GPUs does it support?"
```

Expected loop:
- Iteration 1: model calls `web.search` with "latest ROCm version".
- Iteration 2: model calls `web.fetch` on the most relevant result.
- Iteration 3: model synthesizes the answer.

Total: ~3 iterations, ~5k tokens, $0.00.

### Example 3: Delegating to a smarter model

```
$ orch "Write a careful explanation of how MoE models work, suitable for a smart non-ML reader. About 400 words."
```

The local model could attempt this itself, but the orchestrator is trained to
recognize "careful writing" as a Claude job.

Expected loop:
- Iteration 1: model calls `llm.call` (model="claude") with the task description.
- Iteration 2: model receives Claude's response, decides it's good, returns it.

Total: ~2 iterations, ~2k local tokens + ~500 prompt / 600 completion on Claude
Sonnet = ~$0.011.

### Example 4: Multi-LLM workflow

```
$ orch "Have Claude write a 4-line poem about local LLMs. Then have Gemini critique it. Then revise based on the critique."
```

Expected loop:
- Iter 1: `llm.call` model="claude" (write poem).
- Iter 2: `llm.call` model="gemini_pro" (critique).
- Iter 3: `llm.call` model="claude" (revise based on critique).
- Iter 4: final answer.

Total: ~4 iterations, ~$0.02.

This is the "orchestrator as conductor" pattern — three independent model
calls, each with self-contained context, coordinated by the local model.

### Example 5: Research with budget pressure

```
$ orch --max-cost 0.05 "Compare RDNA3 vs RDNA4 architectural changes in 3 paragraphs. Use the web."
```

The orchestrator sees a cost ceiling and should:
- Iter 1: `web.search "RDNA3 RDNA4 architecture differences"`.
- Iter 2: `web.fetch` on AnandTech / Tom's / similar.
- Iter 3: `llm.call` model="haiku" (cheap!) to draft, not Sonnet.
- Iter 4: final answer.

If the loop tries to use Opus and would exceed $0.05, the budget check at the
start of the next iteration kills it gracefully.

### Example 6: Hitting the loop guard

```
$ orch "What is 2 + 2? Use the code tool. Then use the code tool again to verify. Then verify the verification. Then verify that."
```

A stubborn user demanding redundancy. After two identical `code.execute` calls
with `print(2+2)`, the third gets rejected by the loop guard, forcing the
model to stop or vary the args. Look at the trace afterwards to see this play out:

```
$ orch --trace <run_id_prefix>
```

### What you'll typically see

After a week of using this, you'll notice:
- Most queries resolve in 1–3 iterations.
- The biggest cost driver is large tool results, not the orchestrator.
- The local model occasionally calls a cloud LLM when it could have answered
  directly. The system prompt's "only when needed" is a hint, not enforcement.
- Web fetches against modern JS-heavy pages return junk. We'll fix that in
  phase 6 with a real readability extractor.

---

## 5. Setup, step by step

This section assumes a fresh Arch Linux install on the R9700 box. If you're
already running services there, adjust paths.

### 5.1. Verify the hardware is recognized

```bash
# Both R9700s should appear
lspci | grep -i 'amd\|radeon' | grep -i vga

# Kernel should see both via amdgpu
dmesg | grep amdgpu | head -20

# Vulkan should enumerate both
vulkaninfo --summary | grep -A2 'GPU'
```

If `vulkaninfo` doesn't list both cards, stop here and debug. Common issues:
- User not in `video` and `render` groups → `sudo usermod -aG video,render $USER`
- Missing `vulkan-radeon` package → `sudo pacman -S vulkan-radeon vulkan-icd-loader`
- Kernel too old for RDNA4 → boot the latest stable kernel.

### 5.2. Install system dependencies

```bash
sudo pacman -S --needed \
    python python-pip \
    vulkan-radeon vulkan-icd-loader vulkan-tools \
    base-devel cmake git ninja \
    firejail sqlite
```

`firejail` is what sandboxes `code.execute`. If you skip it, the tool runs
without isolation, which is fine for personal use but you'll see a warning in
the logs.

### 5.3. llama.cpp — reuse your existing build

The orchestrator does **not** compile its own llama.cpp. It reuses the
`llama-server` binary you already build and maintain with `build_tools.sh` —
on this box that's the ROCm build at:

```
/srv/llama/llama.cpp-rocm/build/bin/llama-server
```

`start-llama.sh` (Section 8) defaults to exactly that path. If you don't have it
yet, build it once:

```bash
/srv/llama/build_tools.sh llama rocm     # gfx1201 / RDNA4 ROCm build
# sanity check:
/srv/llama/llama.cpp-rocm/build/bin/llama-server --version
```

> **Keeping it current.** `build_tools.sh` owns compilation; everything else
> (`serve.sh`, `sd-serve.sh`, the orchestrator) just references the binaries it
> produces. After a `pacman -Syu` that touches ROCm, the kernel, or Mesa, the
> existing binary may ABI-mismatch the new libraries — re-run `build_tools.sh`
> as usual, then `orchstop && orchstart` so the service picks up the rebuilt
> binary. The orchestrator adds nothing to this ritual; it consumes the output
> you already maintain.

A new orchestrator-owned build would be the wrong move: a second tree you'd have
to remember to rebuild after every ROCm bump, which silently rots and then
segfaults weeks later. One build pipeline, many launch profiles.

### 5.4. Download a model

You want Qwen3-30B-A3B-Instruct in GGUF format. The `IQ4_XS` quant is ~15GB —
good quality, fits with lots of headroom.

```bash
sudo mkdir -p /srv/models && sudo chown $USER /srv/models
cd /srv/models
# Use huggingface-cli or wget. Replace with current URL from the model card.
huggingface-cli download unsloth/Qwen3-30B-A3B-Instruct-GGUF \
    Qwen3-30B-A3B-Instruct-IQ4_XS.gguf \
    --local-dir /srv/models
```

### 5.5. Install the orchestrator

The runtime and the LiteLLM proxy get **separate virtualenvs**, both on Python
3.13, **both inside the project**. This isn't fussiness on two counts. First,
LiteLLM's `[proxy]` extra pins exact versions of `httpx`, `pydantic`, `tiktoken`,
`tokenizers`, and `aiohttp` — the same packages the runtime (and any model
tooling like `llmenv`) depend on; one shared venv lets a later `pip install`
silently move a pin out from under the other side. Second, keeping both venvs
under `/srv/orchestrator/` makes the project a **self-contained unit**: delete or
rebuild `/srv/orchestrator` and nothing is stranded elsewhere wondering what it
was for. Two venvs, same interpreter, zero conflicts, one tidy boundary.

> **Why 3.13 specifically?** LiteLLM declares `python = ">=3.9,<4.0"` and is
> built/published on CPython 3.13, so it's the safe choice. Avoid 3.14 for now —
> the proxy currently fails to start there (uvloop's `BaseDefaultEventLoopPolicy`
> import error, plus orjson/grpcio wheel issues). The *core* library works on
> 3.14; the *proxy* doesn't yet.

```bash
# From wherever you extracted the tarball
sudo mkdir -p /srv/orchestrator && sudo chown $USER /srv/orchestrator
cp -r orchestrator/* /srv/orchestrator/
cd /srv/orchestrator

# Both venvs: uv + Python 3.13. NOTE uv-created venvs have NO `pip` inside —
# install with `uv pip --python` (or pass --seed to `uv venv` for a real pip).

# Venv 1 — the runtime (agent loop + tools). No LiteLLM here.
uv venv /srv/orchestrator/.venv --python 3.13
uv pip install --python /srv/orchestrator/.venv/bin/python -r requirements.txt

# Venv 2 — the LiteLLM proxy ONLY, isolated and project-local (kept inside
# /srv/orchestrator so the project stays self-contained).
uv venv /srv/orchestrator/litellmenv --python 3.13
uv pip install --python /srv/orchestrator/litellmenv/bin/python -r requirements-litellm.txt

# Once it runs, pin the exact working set so a reinstall is reproducible:
#   uv pip freeze --python /srv/orchestrator/litellmenv/bin/python > requirements-litellm.lock
```

The proxy runs **stateless** — there's no `database_url` in `litellm.yaml`, so
the base `[proxy]` extra is all you need (no Prisma, no Postgres). Cost and usage
are tracked by the orchestrator's own `trace.db`, so LiteLLM's spend DB would be
redundant. (If you ever wanted LiteLLM's DB features — persistent virtual keys,
its spend dashboard — you'd add a `database_url` *and* install
`litellm[proxy,prisma]` with a real database. The minimal setup here skips all
of that on purpose.)

### 5.6. Configure secrets

```bash
mkdir -p ~/.config
cat > ~/.config/orchestrator.env <<EOF
# Providers in use (Claude + Gemini + Qwen)
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
DASHSCOPE_API_KEY=...                  # Qwen (Alibaba Model Studio)
TAVILY_API_KEY=tvly-...                # optional: web.search/fetch backend (else SearxNG/DDG)
# Proxy auth (shared by the proxy + every client)
LITELLM_MASTER_KEY=sk-local-orch-$(openssl rand -hex 16)
EOF
chmod 600 ~/.config/orchestrator.env
```

You don't need a key for every model — leave the ones you don't have unset and
don't call those models. A model in `litellm.yaml` whose key is missing only
errors when it's actually invoked, not at startup, so an unused entry is
harmless. When you add a provider later, keep the set in sync across three
places: `litellm.yaml` (the `model_name`s), `runtime.yaml` (the `costs:` rows),
and the model-selection guidance in the system prompt.

### 5.7. Set up systemd user services

```bash
mkdir -p ~/.config/systemd/user
cp /srv/orchestrator/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now llama-orchestrator.service
systemctl --user enable --now litellm-proxy.service
```

Watch them come up:
```bash
journalctl --user -u llama-orchestrator.service -f
# In another shell:
journalctl --user -u litellm-proxy.service -f
```

The llama-server takes ~30 seconds to load the model into VRAM. You'll see
`HTTP server is listening` when it's ready.

### 5.8. Smoke test

```bash
# Check the local model directly
curl -s http://127.0.0.1:8090/v1/models | python -m json.tool

# Check the LiteLLM proxy
curl -s http://127.0.0.1:4000/v1/models \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" | python -m json.tool

# Run the agent
/srv/orchestrator/scripts/orch "What is 7 factorial? Use the code tool."
```

If all three work, you're done.

### 5.9. Shell aliases

Add to `~/.zshrc` or `~/.bashrc`:

```bash
alias orchenv='source /srv/orchestrator/.venv/bin/activate && set -a && source ~/.config/orchestrator.env && set +a'
alias litellmenv='source /srv/orchestrator/litellmenv/bin/activate'
alias orch='/srv/orchestrator/scripts/orch'
alias orchlogs='journalctl --user -u llama-orchestrator.service -u litellm-proxy.service -f'
alias orchstop='systemctl --user stop llama-orchestrator.service litellm-proxy.service'
alias orchstart='systemctl --user start llama-orchestrator.service litellm-proxy.service'
```

---

**Build it yourself — the two requirements files, the env file, and the systemd units**

*Goal:* the operational glue that makes the two daemons start on login and stay
up. Section 5's prose walks the commands; here are the actual files those
commands install.

*What to write:*

1. **`requirements.txt`** — the *runtime* deps only. `httpx` is the async HTTP
   client the loop and tools use; `click` + `rich` power the CLI (Section 6);
   `pyyaml` parses the configs; `aiosqlite`/`pydantic` round it out. Note what's
   **not** here: `litellm`. It lives in its own venv (next file) so its pinned
   deps can't collide with the runtime's or your model tooling's.
2. **`requirements-litellm.txt`** — just `litellm[proxy]`. Installed into the
   dedicated `litellmenv` (Python 3.13). After a working install, freeze it to a
   `.lock` for reproducibility.
3. **`~/.config/orchestrator.env`** — the secrets, `chmod 600`. Both systemd
   units load this via `EnvironmentFile`. The `LITELLM_MASTER_KEY` is generated
   once and shared by the proxy and every client. Only the providers you use.
4. **`systemd/llama-orchestrator.service`** — runs `scripts/start-llama.sh`
   (built in Section 8). `Restart=on-failure`. Note the comment: the user must
   be in the `video` and `render` groups for Vulkan DRI access.
5. **`systemd/litellm-proxy.service`** — runs `litellm` **from `litellmenv`**
   (not `.venv`) against `litellm.yaml`. `After=...llama-orchestrator.service` so
   the local backend is up first (the fallbacks still work if it isn't, but
   ordering avoids a cold-start error on boot).

*Watch out for:* the proxy unit's `ExecStart` points at
`/srv/orchestrator/litellmenv/bin/litellm`, **not** `.venv` — that's the whole
point of the split. These are **user** units (`systemctl --user`,
`WantedBy=default.target`), not system units — they run as you, which is what
you want for `$HOME`-relative paths and the env file at `%h/.config/...`. If you
want them to start before you log in, you need `loginctl enable-linger $USER`.

<details>
<summary>Reference — <code>requirements.txt</code> (runtime venv)</summary>

```
# Runtime deps (agent loop + tools). LiteLLM is NOT here — see requirements-litellm.txt.
httpx>=0.27.0
pydantic>=2.7.0
pyyaml>=6.0
rich>=13.7.0
click>=8.1.0
aiosqlite>=0.20.0
```

</details>

<details>
<summary>Reference — <code>requirements-litellm.txt</code> (litellmenv)</summary>

```
# LiteLLM proxy deps — installed into a DEDICATED venv (litellmenv), never
# shared with the runtime venv or your model tooling (llmenv/vllmenv).
# The [proxy] extra pins exact versions of httpx, pydantic, tiktoken,
# tokenizers, aiohttp, etc.; isolating it prevents resolver conflicts.
#
# The proxy runs STATELESS here (no database_url in litellm.yaml), so the base
# [proxy] extra is enough — no prisma/Postgres. Cost is tracked by the
# orchestrator's own trace.db. (If you ever add a database_url, LiteLLM needs
# prisma + a real DB: litellm[proxy,prisma].)
#
# Python 3.13 is the sweet spot (3.14 currently breaks the proxy via uvloop).
# After a working install, re-pin exact versions:
#   uv pip freeze > requirements-litellm.lock
litellm[proxy]>=1.83.0
```

</details>

<details>
<summary>Reference — <code>~/.config/orchestrator.env</code></summary>

```bash
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
DASHSCOPE_API_KEY=...                  # Qwen (Alibaba Model Studio)
TAVILY_API_KEY=tvly-...                # optional: web.search/fetch backend (else SearxNG/DDG)
LITELLM_MASTER_KEY=sk-local-orch-...   # openssl rand -hex 16
```

</details>

<details>
<summary>Reference — <code>systemd/llama-orchestrator.service</code></summary>

```ini
[Unit]
Description=llama.cpp server (orchestrator, dual R9700 Vulkan)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/orchestrator.env
ExecStart=/srv/orchestrator/scripts/start-llama.sh
Restart=on-failure
RestartSec=10
# Vulkan needs DRI access; user must be in `video` and `render` groups.
# No additional ambient capabilities required.

[Install]
WantedBy=default.target
```

</details>

<details>
<summary>Reference — <code>systemd/litellm-proxy.service</code></summary>

```ini
[Unit]
Description=LiteLLM proxy (unified OpenAI API for local + cloud LLMs)
After=network-online.target llama-orchestrator.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/orchestrator.env
WorkingDirectory=/srv/orchestrator
ExecStart=/srv/orchestrator/litellmenv/bin/litellm \
    --config /srv/orchestrator/config/litellm.yaml \
    --host 127.0.0.1 \
    --port 4000 \
    --num_workers 1
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

</details>

---

## 6. Usage and observability

### 6.1. The CLI

```bash
orch "your message here"                      # basic
orch --max-cost 0.10 "..."                    # cap cost
orch --max-iterations 5 "..."                 # cap loop length
orch --share-private "..."                    # allow private tool results to remote LLMs
orch --json-output "..."                      # machine-readable result
orch --trace abc12345                          # inspect a prior run
```

The default budgets in `config/runtime.yaml` are conservative: 10 iterations,
5 minutes, $1.00, 200k tokens. Override per-request when you need to.

### 6.2. The trace database

Every run is logged to `/srv/orchestrator/data/trace.db`. You can SQL it directly:

```bash
sqlite3 /srv/orchestrator/data/trace.db

# Most recent 10 runs
SELECT id, status, user_message FROM runs ORDER BY started_at DESC LIMIT 10;

# Cost by run
SELECT id,
       json_extract(summary_json, '$.cost_usd') AS cost,
       json_extract(summary_json, '$.iterations') AS iters
  FROM runs WHERE status='ok' ORDER BY started_at DESC LIMIT 20;

# Step-by-step replay
SELECT iteration, kind, payload_json
  FROM events WHERE run_id='abc...' ORDER BY ts;
```

Or use the CLI:
```bash
orch --trace abc12345
```

### 6.3. Watching live

```bash
orchlogs           # follow both services' logs
```

llama-server's logs are noisy — every token generation can produce output if
debug logging is on. The default config disables that. If you want to see the
raw prompts and completions, look in the LiteLLM logs (it logs every request
it routes).

### 6.4. Spend tracking

Since the proxy runs stateless (no `database_url`), there's no LiteLLM spend DB.
Cost is tracked where it belongs for this project — the orchestrator's own
`trace.db`, which records token usage and computed cost for every run via the
`budget.summary()` written by `trace.finish_run()` (Section 7, Stop 4). Query it
directly:

```bash
# Total cost and run count per status
sqlite3 /srv/orchestrator/data/trace.db \
    "SELECT status, COUNT(*), ROUND(SUM(json_extract(summary_json,'$.cost_usd')),4) AS usd
     FROM runs GROUP BY status;"

# Per-model token usage from the event log
sqlite3 /srv/orchestrator/data/trace.db \
    "SELECT json_extract(payload_json,'$.tool') AS tool,
            COUNT(*) AS calls
     FROM events WHERE kind='tool_result' GROUP BY tool ORDER BY calls DESC;"
```

The `orch --trace <run_id>` command (Section 6.1) is the per-run view of the same
data. If you later want LiteLLM's own per-key spend dashboard and virtual-key
management, that's the point where you'd add a `database_url` and install
`litellm[proxy,prisma]` with a real database — but the local single-user setup
doesn't need it.

### 6.5. When things go wrong

| Symptom | Likely cause | Where to look |
|---------|-------------|---------------|
| `connection refused :8090` | llama-server not up | `journalctl --user -u llama-orchestrator -e` |
| `connection refused :4000` | LiteLLM proxy not up | `journalctl --user -u litellm-proxy -e` |
| Model produces invalid JSON tool calls | Jinja template mismatch | Check `--chat-template-file` path, or drop the flag and use the model's embedded template |
| Tool not appearing in registry | Import error in tool file | Look for `Failed to import tools.X.Y` in logs |
| `BudgetExceeded: max_iterations` | Loop guard or model not converging | Raise `--max-iterations`, or inspect trace to see what's looping |
| `PrivacyViolation` | Trying to call cloud LLM after using private tool | Add `--share-private` or restructure |
| Slow first token after restart | KV cache cold | Normal; warms after 1 query |
| HTTP 500 from Anthropic via LiteLLM | API key wrong or rate limited | Check `~/.config/orchestrator.env` |

---

**Build it yourself — `scripts/orch` (the CLI driver)**

*Goal:* the executable that everything in this section drives. It parses flags,
instantiates `AgentRuntime`, runs one request, and pretty-prints the result —
plus a `--trace` mode that reads the trace DB back out.

*What to write:*

1. **Path bootstrap.** Before importing `runtime.loop`, insert
   `/srv/orchestrator` onto `sys.path` so the script runs from anywhere.
2. **A `click` command** with the flags documented in 6.1: `--max-iterations`,
   `--max-wall-clock`, `--max-cost`, `--share-private`, `--json-output`,
   `--trace`, and a positional `message`. Collect the budget flags into an
   `overrides` dict (only the ones actually set) and pass them to `run()`.
3. **Run and render.** `asyncio.run(runtime.run(...))`, then either dump JSON
   (`--json-output`) or print a `rich` panel coloured by status
   (green/yellow/red for ok/budget_exceeded/error) plus a dim one-line budget
   summary (iterations, elapsed, cost, tokens, cached).
4. **`--trace <id>` mode.** A separate path that opens `data/trace.db`, does a
   prefix `LIKE` match on the run id (so you can paste the first 8 chars from
   the panel title), prints the run row, then each event as syntax-highlighted
   JSON. This is the read side of the trace writer from Stop 4.

*Watch out for:* make the file executable (`chmod +x scripts/orch`) and give it
a `#!/usr/bin/env python` shebang so the `orch` alias works without `python`
in front. The budget overrides must use the *config keys*
(`max_wall_clock_s`), not the CLI flag names (`max-wall-clock`).

<details>
<summary>Reference implementation — <code>scripts/orch</code></summary>

```python
#!/usr/bin/env python
"""Command-line driver for the orchestrator.

Usage:
    orch "What's the weather in Zurich and is it warmer than London?"
    orch --max-cost 0.10 "Cheap quick question"
    orch --share-private "Use AZEK content with cloud LLMs"
    orch --trace 1a2b3c "Show me the trace of this run"
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

# Ensure /srv/orchestrator is on sys.path when running this script directly.
ORCH_ROOT = Path("/srv/orchestrator")
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))

from runtime.loop import AgentRuntime  # noqa: E402

console = Console()


@click.command()
@click.argument("message", required=False)
@click.option("--max-iterations", type=int, help="Override iteration ceiling.")
@click.option("--max-wall-clock", type=float, help="Override wall-clock ceiling (seconds).")
@click.option("--max-cost", type=float, help="Override cost ceiling (USD).")
@click.option("--share-private", is_flag=True,
              help="Allow private tool results to be passed to remote LLMs.")
@click.option("--json-output", "json_out", is_flag=True,
              help="Output the full result dict as JSON instead of pretty-printing.")
@click.option("--trace", "trace_run_id", help="Show the trace of a previous run by id.")
def cli(message, max_iterations, max_wall_clock, max_cost, share_private,
        json_out, trace_run_id):
    if trace_run_id:
        return _show_trace(trace_run_id)

    if not message:
        console.print("[red]Error:[/red] need a message (or --trace <run_id>)")
        sys.exit(2)

    overrides = {}
    if max_iterations is not None:
        overrides["max_iterations"] = max_iterations
    if max_wall_clock is not None:
        overrides["max_wall_clock_s"] = max_wall_clock
    if max_cost is not None:
        overrides["max_cost_usd"] = max_cost

    runtime = AgentRuntime()
    result = asyncio.run(runtime.run(
        message,
        share_private=share_private,
        budget_overrides=overrides or None,
    ))

    if json_out:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    status_color = {"ok": "green", "budget_exceeded": "yellow", "error": "red"}.get(
        result["status"], "white",
    )
    console.print(Panel(result["answer"] or "(empty)",
                        title=f"[{status_color}]{result['status']}[/{status_color}]"
                              f" — run {result['run_id'][:8]}",
                        border_style=status_color))
    b = result["budget"]
    console.print(
        f"[dim]iterations: {b['iterations']}  "
        f"elapsed: {b['elapsed_s']}s  "
        f"cost: ${b['cost_usd']:.4f}  "
        f"tokens: {b['tokens']['total']} "
        f"(cached: {b['tokens']['cached']})[/dim]"
    )
    if result.get("error"):
        console.print(f"[red]error: {result['error']}[/red]")


def _show_trace(run_id: str) -> None:
    import sqlite3
    db = sqlite3.connect("/srv/orchestrator/data/trace.db")
    db.row_factory = sqlite3.Row

    # Allow prefix match for convenience
    runs = db.execute(
        "SELECT * FROM runs WHERE id LIKE ? ORDER BY started_at DESC LIMIT 1",
        (run_id + "%",),
    ).fetchall()
    if not runs:
        console.print(f"[red]no run matching {run_id}[/red]")
        sys.exit(1)
    run = runs[0]
    console.print(Panel(
        f"id: {run['id']}\nstatus: {run['status']}\n"
        f"user: {run['user_message']}\n\nanswer: {run['final_answer']}",
        title="Run",
    ))

    events = db.execute(
        "SELECT * FROM events WHERE run_id = ? ORDER BY ts",
        (run["id"],),
    ).fetchall()
    for ev in events:
        payload = json.loads(ev["payload_json"]) if ev["payload_json"] else {}
        console.print(f"[dim]{ev['iteration']}.[/dim] [cyan]{ev['kind']}[/cyan]")
        console.print(Syntax(json.dumps(payload, indent=2, ensure_ascii=False)[:2000],
                             "json", theme="ansi_dark", word_wrap=True))


if __name__ == "__main__":
    cli()
```

</details>

---

## 7. How the code works (a guided tour)

The codebase is ~1100 lines of Python plus YAML/Jinja config. Read in this order.

### Stop 1: `runtime/tool_base.py` (~90 lines)

This is the contract. Every tool subclasses `Tool` and implements:

```python
class MyTool(Tool):
    name = "namespace.verb"
    description = "What this does (the model reads this)"
    parameters = { "type": "object", "properties": {...}, "required": [...] }

    async def execute(self, args, context) -> ToolResult:
        ...
        return ToolResult(status="ok", result=...)
```

`ToolResult` is the normalized envelope. The `to_model_message()` method
serializes it for the model with a soft size cap — a defense against runaway
context.

**Build it yourself — `runtime/tool_base.py`**

*Goal:* define the three types every other file depends on: `ToolResult`
(what tools return), `Tool` (what tools subclass), and `ToolContext` (what
tools receive). Build this first — nothing else compiles without it.

*What to write:*

1. **`ToolResult` dataclass.** Fields: `status` (`"ok"`/`"error"`), `result`
   (any payload), `tool_name`, `error`, plus runtime-filled bookkeeping
   (`tokens_used` dict, `cost_usd`, `latency_ms`) and a `private` flag. Add
   `to_model_message()` that JSON-serializes the result and **soft-caps it at
   ~20k chars** — this is the per-result defense against the context blow-up
   from 2.5. On error, emit a compact error object instead.
2. **`Tool` ABC.** Class attributes `name`, `description`, `parameters` (JSON
   Schema), and the two policy flags `private` and `requires_confirmation`. One
   abstract async method `execute(args, context) -> ToolResult`. A concrete
   `to_openai_schema()` that wraps `name`/`description`/`parameters` in the
   `{"type": "function", "function": {...}}` shape the API wants.
3. **`ToolContext` dataclass.** Per-request state handed to every tool:
   `request_id`, parsed `config`, the `budget`, `share_private`, `trace_id`.

*Watch out for:* `execute` is **async** for every tool, even ones that don't do
I/O — the loop `await`s them uniformly. Use `from __future__ import
annotations` so the forward references to `Budget`/`ToolContext` don't need
imports (avoiding a circular import with `budget.py` and `loop.py`).

<details>
<summary>Reference implementation — <code>runtime/tool_base.py</code></summary>

```python
"""Tool base class and standard result envelope.

Every tool in /srv/orchestrator/tools/ subclasses `Tool` and is auto-discovered
at startup. Tools are async, declare their JSON schema, and return a normalized
`ToolResult` so the runtime can treat them uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """Normalized envelope every tool returns."""

    status: str                              # "ok" | "error"
    result: Any                              # tool-specific payload
    tool_name: str = ""
    error: str | None = None
    # Bookkeeping the runtime fills in:
    tokens_used: dict[str, int] = field(default_factory=dict)  # {prompt, completion, cached}
    cost_usd: float = 0.0
    latency_ms: int = 0
    # Privacy flag — set automatically by the dispatcher when the tool's
    # namespace is in `privacy.private_tool_namespaces`.
    private: bool = False

    def to_model_message(self) -> str:
        """Serialize for the LLM. Truncates huge results to keep context lean."""
        import json
        if self.status == "error":
            return json.dumps({"status": "error", "error": self.error or "unknown"})
        # Keep payloads bounded — protects orchestrator context window.
        payload = self.result
        s = json.dumps({"status": "ok", "result": payload}, ensure_ascii=False, default=str)
        if len(s) > 20000:
            # Soft cap: tools should pre-summarize, but be defensive.
            s = s[:20000] + '..."__truncated__":true}'
        return s


class Tool(ABC):
    """Base class for all tools. Subclass and place in /srv/orchestrator/tools/<namespace>/."""

    # Tool identity — must be unique. Convention: "<namespace>.<verb>"
    name: str = ""
    description: str = ""

    # JSON Schema for arguments (OpenAI tool-call format).
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    # Privacy: if True, results may not be passed to remote LLM tools
    # unless the request has share_private=True.
    private: bool = False

    # Confirmation: if True, runtime pauses for human approval before executing.
    requires_confirmation: bool = False

    @abstractmethod
    async def execute(self, args: dict[str, Any], context: "ToolContext") -> ToolResult:
        """Execute the tool. Must be async; may call external services."""
        raise NotImplementedError

    def to_openai_schema(self) -> dict[str, Any]:
        """Render as an OpenAI tool definition for the chat completion API."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolContext:
    """Per-request context passed to every tool.execute() call."""

    request_id: str
    config: dict[str, Any]                 # parsed runtime.yaml
    budget: "Budget"                       # forward ref; runtime.budget.Budget
    share_private: bool = False            # may private results leave the box?
    trace_id: int | None = None            # current trace row, if logging
```

</details>

### Stop 2: `runtime/registry.py` (~80 lines)

The plugin loader. Walks `tools/**/*.py`, imports each module, finds concrete
`Tool` subclasses, instantiates them. The interesting bit is the `sys.path`
manipulation so that `from runtime.tool_base import Tool` works inside tool
files — the registry inserts the parent of `tools/` into `sys.path` so the
dotted import `tools.web.search_fetch` resolves.

Once you understand this 80 lines, you've understood every plugin system in
Python: pkgutil, entry_points, you name it. The mechanism is always the same.

**Build it yourself — `runtime/registry.py`**

*Goal:* the plugin loader that makes "add a tool = drop a file" true. It walks
`tools/`, imports each module, and instantiates every concrete `Tool` subclass
it finds.

*What to write:*

1. **`ToolRegistry(tools_root)`** holding a `name -> Tool` dict.
2. **`discover()`** — the heart. Insert `tools_root.parent` onto `sys.path`
   (so `tools.web.search_fetch` resolves), then `rglob("*.py")`, skipping files
   whose name starts with `_`. For each, build the dotted module name relative
   to the parent and `importlib.import_module` it inside a try/except that
   **logs and continues** on failure (one broken tool shouldn't sink startup).
3. **Find the classes.** `inspect.getmembers(module, inspect.isclass)`, keep
   only concrete `Tool` subclasses *defined in this module*
   (`obj.__module__ == module.__name__` — this excludes the imported base
   `Tool` itself) and not abstract. Instantiate; skip empties; warn on
   duplicate names; register.
4. **Accessors:** `get(name)`, `all()`, and `openai_schemas(allowed=None)` which
   renders the (optionally allow-listed) tools as API schemas. The `allowed`
   param is what makes per-agent tool restriction trivial in phase 7.

*Watch out for:* the `obj.__module__ == module.__name__` check is the subtle
bit — without it, every module that does `from runtime.tool_base import Tool`
would re-register the abstract base. The `_`-prefix skip is also why
`__init__.py` files are ignored automatically.

<details>
<summary>Reference implementation — <code>runtime/registry.py</code></summary>

```python
"""Tool registry with plugin auto-discovery.

Scans /srv/orchestrator/tools/<namespace>/*.py at startup, imports each module,
and registers any concrete Tool subclasses found.

To add a new tool:
1. Create /srv/orchestrator/tools/<namespace>/<verb>.py
2. Subclass Tool, set name = "<namespace>.<verb>", implement execute()
3. Restart the runtime (or call registry.reload())
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
from pathlib import Path

from .tool_base import Tool

log = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self, tools_root: str | Path):
        self.tools_root = Path(tools_root)
        self._tools: dict[str, Tool] = {}

    def discover(self) -> None:
        """Walk the tools directory and import every .py file (except __init__)."""
        self._tools.clear()
        if not self.tools_root.exists():
            log.warning("Tools root does not exist: %s", self.tools_root)
            return

        # Ensure parent of tools/ is importable so "tools.<ns>.<mod>" resolves.
        parent = str(self.tools_root.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)

        for py_file in sorted(self.tools_root.rglob("*.py")):
            if py_file.name.startswith("_"):
                continue
            # Build module path relative to tools_root.parent
            rel = py_file.relative_to(self.tools_root.parent).with_suffix("")
            mod_name = ".".join(rel.parts)
            try:
                module = importlib.import_module(mod_name)
            except Exception as e:
                log.error("Failed to import %s: %s", mod_name, e)
                continue

            for _, obj in inspect.getmembers(module, inspect.isclass):
                # Concrete Tool subclasses defined in this module (not imports)
                if (issubclass(obj, Tool) and obj is not Tool
                        and obj.__module__ == module.__name__
                        and not inspect.isabstract(obj)):
                    instance = obj()
                    if not instance.name:
                        log.warning("Tool class %s has empty name, skipping", obj)
                        continue
                    if instance.name in self._tools:
                        log.warning("Duplicate tool name %s (replacing)", instance.name)
                    self._tools[instance.name] = instance
                    log.info("Registered tool: %s", instance.name)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def openai_schemas(self, allowed: list[str] | None = None) -> list[dict]:
        """Render tools as OpenAI tool definitions."""
        tools = self._tools.values() if allowed is None else \
                [t for n, t in self._tools.items() if n in allowed]
        return [t.to_openai_schema() for t in tools]
```

</details>

### Stop 3: `runtime/budget.py` (~95 lines)

Pure bookkeeping. `tick()` is called at the start of each loop iteration and
checks all four ceilings. `add_usage()` accumulates tokens and converts to
cost via the table in `runtime.yaml`. When any limit is hit, `BudgetExceeded`
is raised with structured details.

Note the `cached` token handling: cached input tokens cost 10% of full input
(Anthropic's pricing), so the cost calculation discounts them. This matters
once you're running long loops with stable system prompts.

**Build it yourself — `runtime/budget.py`**

*Goal:* the four-ceiling enforcer from 3.3. One `Budget` per run, consumed as
the loop turns; raises `BudgetExceeded` the moment any limit trips.

*What to write:*

1. **`BudgetExceeded(Exception)`** carrying a `reason` string and a `details`
   dict, so the loop can log *which* ceiling and *by how much*.
2. **`Budget` dataclass** with the four limits plus consumption counters
   (iterations, cost, prompt/completion/cached tokens, `started_at` monotonic).
   Properties: `elapsed_s`, `total_tokens`.
3. **`tick()`** — bump iteration count and call `check()`. Called once at the
   top of every loop pass.
4. **`add_usage(model, prompt, completion, cached, cost_table)`** — accumulate
   tokens and convert to cost. Key subtlety: **billable prompt = (prompt −
   cached) + cached × 0.1**, the 10% cached-input discount from 2.5. Divide by
   1e6 because the cost table is per-million.
5. **`check()`** — raise `BudgetExceeded` if any of the four limits is exceeded.
6. **`summary()`** — a dict for the trace and the CLI footer.

*Watch out for:* use `time.monotonic()`, not `time.time()`, for elapsed — you
want a clock that can't jump backwards. And `check()` after the increment in
`tick()` means `max_iterations: 10` allows iterations 1–10 and trips on 11; pick
your off-by-one and document it.

<details>
<summary>Reference implementation — <code>runtime/budget.py</code></summary>

```python
"""Budget tracking for an agent run.

A `Budget` is created per request and consumed as the loop runs. When any
ceiling is hit, `check()` raises `BudgetExceeded` and the loop terminates
gracefully.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    """Raised when any budget ceiling is hit."""

    def __init__(self, reason: str, details: dict):
        super().__init__(reason)
        self.reason = reason
        self.details = details


@dataclass
class Budget:
    max_iterations: int
    max_wall_clock_s: float
    max_cost_usd: float
    max_total_tokens: int

    # Consumption counters
    iterations: int = 0
    cost_usd: float = 0.0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_cached: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def total_tokens(self) -> int:
        return self.tokens_prompt + self.tokens_completion

    def tick(self) -> None:
        """Call at the start of each loop iteration."""
        self.iterations += 1
        self.check()

    def add_usage(self, model: str, prompt: int, completion: int,
                  cached: int = 0, cost_table: dict | None = None) -> None:
        """Record token usage and update cost estimate."""
        self.tokens_prompt += prompt
        self.tokens_completion += completion
        self.tokens_cached += cached
        if cost_table and model in cost_table:
            rates = cost_table[model]
            # Cached input is typically 10% of full input cost (Anthropic).
            billable_prompt = max(0, prompt - cached) + cached * 0.1
            self.cost_usd += (billable_prompt * rates["input"] / 1_000_000)
            self.cost_usd += (completion * rates["output"] / 1_000_000)

    def check(self) -> None:
        """Raise BudgetExceeded if any ceiling is hit."""
        if self.iterations > self.max_iterations:
            raise BudgetExceeded("max_iterations", {
                "iterations": self.iterations, "limit": self.max_iterations,
            })
        if self.elapsed_s > self.max_wall_clock_s:
            raise BudgetExceeded("max_wall_clock_s", {
                "elapsed_s": round(self.elapsed_s, 1), "limit": self.max_wall_clock_s,
            })
        if self.cost_usd > self.max_cost_usd:
            raise BudgetExceeded("max_cost_usd", {
                "cost_usd": round(self.cost_usd, 4), "limit": self.max_cost_usd,
            })
        if self.total_tokens > self.max_total_tokens:
            raise BudgetExceeded("max_total_tokens", {
                "total_tokens": self.total_tokens, "limit": self.max_total_tokens,
            })

    def summary(self) -> dict:
        return {
            "iterations": self.iterations,
            "elapsed_s": round(self.elapsed_s, 2),
            "cost_usd": round(self.cost_usd, 4),
            "tokens": {
                "prompt": self.tokens_prompt,
                "completion": self.tokens_completion,
                "cached": self.tokens_cached,
                "total": self.total_tokens,
            },
        }
```

</details>

### Stop 4: `runtime/trace.py` (~90 lines)

SQLite logger. Two tables: `runs` (one row per request) and `events`
(one row per step). Schema lives in the module as a string — cheap and visible.

The `log_content` flag lets you choose between full content logging
(useful for development) and metadata-only (useful in production where you
don't want sensitive content sitting in a SQLite file).

**Build it yourself — `runtime/trace.py`**

*Goal:* the persistence layer that makes `orch --trace` (Section 6) possible.
Two tables, plain SQLite, no ORM.

*What to write:*

1. **Schema as a string constant.** A `runs` table (id, timestamps,
   user_message, final_answer, status, error, summary_json) and an `events`
   table (auto-increment id, run_id FK, ts, kind, iteration, payload_json),
   plus an index on `(run_id, ts)`. Run it with `executescript` on connect.
2. **`Trace(db_path, log_content=True)`** — `mkdir` the parent, connect with
   `isolation_level=None` (autocommit — simplest for an append-only log).
3. **`start_run` / `finish_run`** — INSERT then UPDATE the `runs` row. When
   `log_content` is false, store empty strings for user_message/final_answer.
4. **`log(run_id, kind, iteration, payload)`** — INSERT an event. When
   `log_content` is false and the kind is a tool call/result, run payload
   through `_strip_content()` first, replacing `result`/`content`/`args` with
   `"<stripped>"` so metadata (latencies, statuses, token counts) survives but
   sensitive bodies don't.

*Watch out for:* the privacy story has two layers — the *dispatcher* gate
(3.4) stops data leaving the box, and `log_content: false` stops it landing in
the trace DB. They're independent; decide both deliberately. JSON-dump payloads
with `default=str` so stray non-serializable objects don't crash logging.

<details>
<summary>Reference implementation — <code>runtime/trace.py</code></summary>

```python
"""Reasoning trace logger.

Every step the agent takes (model turn, tool call, tool result, error) is
logged to SQLite. Useful for debugging, replay, and analyzing cost drivers.

Schema is intentionally simple: one `runs` table for request-level metadata,
one `events` table for the per-step log.
"""

from __future__ import annotations

import json
import time
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    finished_at REAL,
    user_message TEXT,
    final_answer TEXT,
    status TEXT,           -- "ok" | "error" | "budget_exceeded"
    error TEXT,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,    -- "model_turn" | "tool_call" | "tool_result" | "error"
    iteration INTEGER,
    payload_json TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, ts);
"""


class Trace:
    def __init__(self, db_path: str | Path, log_content: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_content = log_content
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.executescript(SCHEMA)

    def start_run(self, run_id: str, user_message: str) -> None:
        self._conn.execute(
            "INSERT INTO runs (id, started_at, user_message, status) VALUES (?, ?, ?, ?)",
            (run_id, time.time(), user_message if self.log_content else "", "running"),
        )

    def finish_run(self, run_id: str, status: str, final_answer: str = "",
                   error: str = "", summary: dict | None = None) -> None:
        self._conn.execute(
            """UPDATE runs SET finished_at=?, status=?, final_answer=?, error=?, summary_json=?
               WHERE id=?""",
            (time.time(), status,
             final_answer if self.log_content else "",
             error,
             json.dumps(summary) if summary else None,
             run_id),
        )

    def log(self, run_id: str, kind: str, iteration: int, payload: Any) -> int:
        """Record an event. Returns the event id."""
        if not self.log_content and kind in ("tool_call", "tool_result"):
            # Strip large content fields, keep metadata
            payload = self._strip_content(payload)
        cur = self._conn.execute(
            "INSERT INTO events (run_id, ts, kind, iteration, payload_json) VALUES (?, ?, ?, ?, ?)",
            (run_id, time.time(), kind, iteration,
             json.dumps(payload, default=str, ensure_ascii=False)),
        )
        return cur.lastrowid

    @staticmethod
    def _strip_content(payload: Any) -> Any:
        if isinstance(payload, dict):
            return {k: ("<stripped>" if k in ("result", "content", "args") else v)
                    for k, v in payload.items()}
        return payload

    def close(self) -> None:
        self._conn.close()
```

</details>

### Stop 5: `runtime/loop.py` (~290 lines) — the main event

The `AgentRuntime.run()` method is the core. Walk through it slowly:

1. **Initialization**: build a `Budget`, start a `Trace`, prepare the messages
   list with system prompt + user message, fetch the tool schemas from the
   registry.

2. **The main `while True`** loop. Each iteration:
   - `budget.tick()` — bumps iteration count and checks ceilings.
   - `_model_turn()` — one POST to LiteLLM, returns the assistant message and
     usage stats.
   - Update budget with the orchestrator's own token use.
   - If no tool calls → final answer, break.
   - Otherwise, for each tool call:
     - Loop guard: hash the (name, args) pair. If seen twice already, refuse.
     - Privacy gate: if the tool is a remote LLM tool and any prior message is
       "tainted" (came from a private tool), refuse unless `share_private`.
     - Execute the tool via `_execute_tool()`.
     - Append the result as a `role: "tool"` message; mark the index as
       tainted if the tool was private.

3. **Exit paths**: normal completion, `BudgetExceeded`, `PrivacyViolation`,
   or unexpected exception. Each is logged distinctly.

The whole thing is ~200 lines of actual logic. Read it twice. Once you can
trace one iteration in your head, you understand agents.

**Build it yourself — `runtime/loop.py`**

*Goal:* the agent loop itself — the file every other file exists to serve. This
is where 2.4's diagram becomes code. Build it last of the runtime files; it
depends on all four others.

*What to write:*

1. **`AgentRuntime.__init__`** — load `runtime.yaml`; build and `discover()` the
   registry; mark tools whose namespace is in `private_tool_namespaces` as
   `private = True`; read the system prompt file; open the `Trace`; cache the
   model name, LiteLLM base, and cost table.
2. **`run(user_message, share_private, budget_overrides)`** — the public entry:
   - Mint a `run_id` (uuid), build a `Budget` from config + overrides,
     `trace.start_run`.
   - Seed `messages` with system + user; init two pieces of loop state:
     `private_taint: set[int]` (message indices derived from private tools) and
     `recent_calls: list[str]` (for the loop guard).
   - **`while True`:** `budget.tick()`; one `_model_turn()`; log it; feed the
     orchestrator's own token usage into the budget; append the assistant
     message. **No `tool_calls` → that's the final answer; break.** Otherwise,
     for each tool call: parse args (catch bad JSON → error result); **loop
     guard** (refuse a (name,args) signature seen ≥2×); **privacy gate**
     (`_enforce_privacy`); execute; log the result; fold any *tool-incurred* LLM
     usage into the budget; append a `role:"tool"` message and taint its index
     if the tool was private.
   - Wrap the loop in try/except for `BudgetExceeded`, `PrivacyViolation`, and
     bare `Exception`, each producing a distinct status + graceful final answer.
     Always `trace.finish_run` and return the result dict.
3. **Helpers:** `_model_turn` (one httpx POST to LiteLLM with
   `tool_choice:"auto"`, returns message + usage); `_execute_tool` (look up,
   await, time, stamp `private` from the tool); `_enforce_privacy` (if not
   `share_private` and the tool is a remote LLM tool and *any* taint exists,
   raise — the deliberately-coarse gate from 3.4); `_call_signature` (stable
   sha256 of name+sorted-args for the loop guard).

*Watch out for:* the budget gets fed **twice** per iteration — once for the
orchestrator's own turn, once for any cloud LLM a tool invoked (the
`result.tokens_used` block). Miss the second and a `call_opus` tool burns money
invisibly. Also: `tool_call_id` must be echoed back on the `role:"tool"`
message or the model can't match results to calls. (Optional next step: §3.7
patches the single `openai_schemas()` line here to add cache-safe, per-run tool
selection — build the base loop first, then layer that on.)

<details>
<summary>Reference implementation — <code>runtime/loop.py</code></summary>

```python
"""Agent reasoning loop.

A bounded Level-1 agent: model proposes tool calls, runtime executes them,
results fed back, repeats until the model produces a final answer or any
budget ceiling is hit.

Key responsibilities:
- Translate between OpenAI tool-call format and our ToolResult envelope
- Enforce privacy: block private tool results from being passed to remote LLMs
- Detect repeat tool calls (same name+args twice) → loop guard
- Update budget on every model turn and tool call
- Log every step to the trace DB
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

from .budget import Budget, BudgetExceeded
from .registry import ToolRegistry
from .tool_base import Tool, ToolContext, ToolResult
from .trace import Trace

log = logging.getLogger(__name__)


class PrivacyViolation(Exception):
    """Orchestrator tried to pass private content to a remote LLM tool."""


class AgentRuntime:
    def __init__(self, config_path: str | Path = "/srv/orchestrator/config/runtime.yaml"):
        self.config_path = Path(config_path)
        with self.config_path.open() as f:
            self.config = yaml.safe_load(f)

        orch_root = self.config_path.parent.parent
        tools_root = orch_root / "tools"
        self.registry = ToolRegistry(tools_root)
        self.registry.discover()

        # Mark tools private based on namespace config
        private_ns = set(self.config["privacy"]["private_tool_namespaces"])
        for tool in self.registry.all():
            ns = tool.name.split(".", 1)[0]
            if ns in private_ns:
                tool.private = True

        prompt_path = orch_root / self.config["orchestrator"]["system_prompt"]
        self.system_prompt = prompt_path.read_text()

        self.trace = Trace(
            self.config["trace"]["db_path"],
            log_content=self.config["trace"]["log_content"],
        )

        self.litellm_base = self.config["orchestrator"]["litellm_base"]
        self.model = self.config["orchestrator"]["model"]
        self.cost_table = self.config["costs"]

    async def run(self, user_message: str, *, share_private: bool = False,
                  budget_overrides: dict | None = None) -> dict:
        """Execute one full agent run. Returns a result dict with answer + metadata."""
        run_id = str(uuid.uuid4())
        b_cfg = {**self.config["budgets"], **(budget_overrides or {})}
        budget = Budget(
            max_iterations=b_cfg["max_iterations"],
            max_wall_clock_s=b_cfg["max_wall_clock_s"],
            max_cost_usd=b_cfg["max_cost_usd"],
            max_total_tokens=b_cfg["max_total_tokens"],
        )

        self.trace.start_run(run_id, user_message)

        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_message},
        ]
        # Track which assistant messages were derived from private tool results.
        # Indexed by message position. Used to enforce privacy on subsequent calls.
        private_taint: set[int] = set()
        # Track recent tool calls for loop detection.
        recent_calls: list[str] = []

        tools_schema = self.registry.openai_schemas()

        ctx = ToolContext(
            request_id=run_id,
            config=self.config,
            budget=budget,
            share_private=share_private,
        )

        final_answer = ""
        status = "ok"
        error_msg = ""

        try:
            while True:
                budget.tick()
                # ---- Model turn ----
                turn = await self._model_turn(messages, tools_schema)
                self.trace.log(run_id, "model_turn", budget.iterations, {
                    "model": self.model,
                    "usage": turn.get("usage", {}),
                    "tool_calls": [
                        {"name": tc["function"]["name"], "args": tc["function"]["arguments"]}
                        for tc in (turn["message"].get("tool_calls") or [])
                    ],
                    "content_len": len(turn["message"].get("content") or ""),
                })

                usage = turn.get("usage", {})
                budget.add_usage(
                    self.model,
                    prompt=usage.get("prompt_tokens", 0),
                    completion=usage.get("completion_tokens", 0),
                    cached=usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                            if isinstance(usage.get("prompt_tokens_details"), dict) else 0,
                    cost_table=self.cost_table,
                )

                msg = turn["message"]
                messages.append(msg)
                tool_calls = msg.get("tool_calls") or []

                # ---- Termination: no tool calls = final answer ----
                if not tool_calls:
                    final_answer = msg.get("content") or ""
                    break

                # ---- Execute tools ----
                for tc in tool_calls:
                    name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"]
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError as e:
                        result = ToolResult(status="error", result=None, tool_name=name,
                                            error=f"invalid JSON args: {e}")
                    else:
                        # Loop guard
                        call_sig = self._call_signature(name, args)
                        if recent_calls.count(call_sig) >= 2:
                            result = ToolResult(
                                status="error", result=None, tool_name=name,
                                error="duplicate tool call detected (loop guard); "
                                      "vary the arguments or stop calling this tool",
                            )
                        else:
                            recent_calls.append(call_sig)
                            if len(recent_calls) > 20:
                                recent_calls.pop(0)
                            # Privacy gate
                            self._enforce_privacy(name, args, messages, private_taint, share_private)
                            result = await self._execute_tool(name, args, ctx)

                    self.trace.log(run_id, "tool_result", budget.iterations, {
                        "tool": name,
                        "status": result.status,
                        "error": result.error,
                        "latency_ms": result.latency_ms,
                        "tokens": result.tokens_used,
                        "private": result.private,
                    })

                    # Update budget with tool's own LLM usage (call_claude etc)
                    if result.tokens_used:
                        budget.add_usage(
                            result.tokens_used.get("model", name),
                            prompt=result.tokens_used.get("prompt", 0),
                            completion=result.tokens_used.get("completion", 0),
                            cached=result.tokens_used.get("cached", 0),
                            cost_table=self.cost_table,
                        )

                    # Append result to conversation
                    msg_idx = len(messages)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": name,
                        "content": result.to_model_message(),
                    })
                    if result.private:
                        private_taint.add(msg_idx)

        except BudgetExceeded as e:
            status = "budget_exceeded"
            error_msg = f"{e.reason}: {e.details}"
            log.warning("Budget exceeded: %s", error_msg)
            final_answer = (
                f"[Run terminated: {e.reason}]\n"
                f"Partial result based on work so far: {final_answer or '(no answer produced yet)'}"
            )
        except PrivacyViolation as e:
            status = "error"
            error_msg = f"privacy_violation: {e}"
            log.warning("Privacy violation: %s", e)
            final_answer = f"[Run terminated: privacy violation. {e}]"
        except Exception as e:
            status = "error"
            error_msg = f"{type(e).__name__}: {e}"
            log.exception("Unexpected error in agent loop")
            final_answer = f"[Internal error: {error_msg}]"

        summary = budget.summary()
        self.trace.finish_run(run_id, status, final_answer, error_msg, summary)

        return {
            "run_id": run_id,
            "status": status,
            "answer": final_answer,
            "error": error_msg or None,
            "budget": summary,
        }

    # ---------- Internal helpers ----------

    async def _model_turn(self, messages: list[dict], tools_schema: list[dict]) -> dict:
        """One call to the local orchestrator model via LiteLLM."""
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{self.litellm_base}/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": tools_schema,
                    "tool_choice": "auto",
                    "temperature": 0.3,
                },
                headers={"Authorization": "Bearer " + self._litellm_key()},
            )
            r.raise_for_status()
            data = r.json()
            return {
                "message": data["choices"][0]["message"],
                "usage": data.get("usage", {}),
            }

    def _litellm_key(self) -> str:
        import os
        return os.environ.get("LITELLM_MASTER_KEY", "sk-local-orch-CHANGE-ME")

    async def _execute_tool(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        tool = self.registry.get(name)
        if tool is None:
            return ToolResult(status="error", result=None, tool_name=name,
                              error=f"unknown tool: {name}")
        start = time.monotonic()
        try:
            result = await tool.execute(args, ctx)
        except Exception as e:
            log.exception("Tool %s raised", name)
            return ToolResult(status="error", result=None, tool_name=name,
                              error=f"{type(e).__name__}: {e}",
                              latency_ms=int((time.monotonic() - start) * 1000))
        if not result.tool_name:
            result.tool_name = name
        if not result.latency_ms:
            result.latency_ms = int((time.monotonic() - start) * 1000)
        result.private = tool.private
        return result

    def _enforce_privacy(self, tool_name: str, args: dict, messages: list[dict],
                         private_taint: set[int], share_private: bool) -> None:
        """Block calling remote LLM tools when conversation contains private content."""
        if share_private:
            return
        if tool_name not in self.config["privacy"]["remote_llm_tools"]:
            return
        # Check if any tool argument value matches content from a tainted message.
        if not private_taint:
            return
        # Conservative check: if there's ANY tainted message in the conversation,
        # refuse the remote LLM call. (A finer-grained per-arg check is possible
        # but error-prone — better to be strict by default.)
        raise PrivacyViolation(
            f"cannot call {tool_name}: conversation contains private tool results "
            f"(from tainted messages). Re-run with --share-private to allow."
        )

    @staticmethod
    def _call_signature(name: str, args: dict) -> str:
        """Stable hash of a tool call for loop detection."""
        s = name + "|" + json.dumps(args, sort_keys=True, default=str)
        return hashlib.sha256(s.encode()).hexdigest()[:16]
```

</details>

### Stop 6: `tools/llm/cloud_models.py` (~180 lines)

A single consolidated tool, `llm.call`, that fronts every cloud model. Rather
than one `Tool` subclass per model (which bloats the schema with N near-identical
definitions — see §3.7), there's one tool whose `model` argument is an enum of
aliases. The orchestrator picks the model by passing `model="haiku"`, `"claude"`,
`"qwen_flash"`, etc.

```python
class CallCloudLLM(Tool):
    name = "llm.call"
    parameters = {"properties": {"model": {"enum": [...]}, "task": {...}, ...}}
    async def execute(self, args, ctx):
        return await _call_via_litellm(args["model"], args["task"], ...)
```

The shared `_call_via_litellm` does the real work: maps the alias to a
`litellm.yaml` model name, builds a self-contained prompt from `task` + optional
`payload`, optionally sets `response_format` for JSON, resolves thinking on/off,
sends to the proxy, and returns a normalized `ToolResult` with token usage.

Three details worth internalizing:

- **No conversation history is forwarded.** The cloud LLM gets only the task
  description. The orchestrator owns the context — this is the privacy and
  token-economy guarantee the whole architecture leans on.
- **Thinking control.** Reasoning models (Qwen3.5, Gemini 3) emit a
  chain-of-thought. For the fast/code tier we disable it by default
  (`enable_thinking: false` for Qwen, `reasoning_effort: none` for Gemini) so a
  trivial task doesn't burn hundreds of reasoning tokens; the orchestrator can
  override per call with `think=true`. We extract only
  `choices[0].message.content`, never the reasoning blocks.
- **One enum, one map.** Adding a provider later is one enum value + one
  `_MODEL_MAP` line + a `litellm.yaml` block + a `costs:` row — no new tool class.

**Build it yourself — `tools/llm/cloud_models.py`**

*Goal:* the consolidated cloud-LLM tool and the shared helper. One tool with a
`model` enum keeps the tool-schema prefix small and cache-friendly (§3.7).

*What to write:*

1. **`_MODEL_MAP`** — alias → `litellm.yaml` model name. The aliases are what the
   orchestrator (and the system prompt) use; the values must match `litellm.yaml`
   `model_name`s *and* the `costs:` keys in `runtime.yaml`.
2. **`_call_via_litellm(alias, task, payload, system, want_json, think, ctx)`** —
   the shared core. Map the alias (error on unknown). Build a *self-contained*
   message list: optional system override, then a user message of `task` (plus
   `payload` joined by `---` if present). Resolve thinking: explicit `think` wins,
   else default off for the fast/code tier (`_THINKING_OFF_BY_DEFAULT`). POST to
   the proxy with the bearer key. On success return the content **and** a
   `tokens_used` dict (model, prompt, completion, cached) so the loop can bill it
   (Stop 5). Extract only `message.content` — never the reasoning blocks.
3. **`CallCloudLLM(Tool)`** with `name = "llm.call"`, a `model` enum covering the
   aliases, and `task`/`payload`/`system`/`format`/`think` params. The per-value
   guidance in the `model` description is load-bearing — it's the orchestrator's
   only hint for picking the right cost tier, so spell out the tiers.

*Watch out for:* keep the alias set in lockstep across three files — the enum
here, `litellm.yaml`'s `model_name`s, and `runtime.yaml`'s `costs:` rows. The
privacy gate in `runtime.yaml` names this tool (`remote_llm_tools: [llm.call]`),
so a rename here must be mirrored there or private results could leak. These
tools import `from runtime.tool_base import ...` (top-level `runtime`, not
relative) because the registry put the project root on `sys.path`.

<details>
<summary>Reference implementation — <code>tools/llm/cloud_models.py</code></summary>

```python
"""Cloud LLM tools — a single consolidated `llm.call` tool.

The orchestrator delegates self-contained subtasks to a cloud model by calling
`llm.call` with a `model` alias chosen by cost/capability. One tool with a
`model` enum (rather than N near-identical tools) keeps the tool-schema prefix
small and cache-friendly — see guide §3.7.

Token-efficiency principles:
- Each call builds a SELF-CONTAINED prompt from `task` (+ optional `payload`).
  No conversation history is forwarded — the orchestrator owns that.
- Optional `system` override and `format: "json"` per call.
- Thinking/reasoning models (Qwen3.5, Gemini) emit a chain-of-thought that we
  do NOT forward to the orchestrator: we read only `choices[0].message.content`.
  For the cheap/fast tier we also DISABLE thinking at the provider so a trivial
  task doesn't burn hundreds of reasoning tokens.
"""

from __future__ import annotations

import os
import time
import httpx

from runtime.tool_base import Tool, ToolContext, ToolResult


_LITELLM_BASE = "http://127.0.0.1:4000"

# alias -> litellm.yaml model_name. Only Claude + Gemini + Qwen are wired up.
_MODEL_MAP = {
    # cheap / fast
    "haiku":        "claude-haiku",
    "gemini_flash": "gemini-flash",
    "qwen_flash":   "qwen-flash",
    # workhorse reasoning / writing
    "claude":       "claude-sonnet",
    "qwen_plus":    "qwen-plus",
    # frontier (costly) — use sparingly
    "opus":         "claude-opus",
    "qwen_max":     "qwen-max",
    # code specialist
    "qwen_coder":   "qwen-coder",
    # long context
    "gemini_pro":   "gemini-pro",
}

# Aliases where we force thinking OFF by default: the fast tier (don't pay for a
# chain-of-thought on cheap tasks) and the code specialist (wants direct output).
# The orchestrator can still override per call via the `think` argument.
_THINKING_OFF_BY_DEFAULT = {"qwen_flash", "qwen_plus", "qwen_coder", "gemini_flash"}


async def _call_via_litellm(alias: str, task: str, payload: str | None,
                            system: str | None, want_json: bool,
                            think: bool | None, ctx: ToolContext) -> ToolResult:
    """Shared implementation. Returns a ToolResult carrying content + token usage."""
    model = _MODEL_MAP.get(alias)
    if model is None:
        return ToolResult(status="error", result=None,
                          error=f"unknown model alias '{alias}'. "
                                f"valid: {', '.join(sorted(_MODEL_MAP))}")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    user_content = task if not payload else f"{task}\n\n---\n\n{payload}"
    messages.append({"role": "user", "content": user_content})

    body: dict = {"model": model, "messages": messages, "temperature": 0.3}
    if want_json:
        body["response_format"] = {"type": "json_object"}

    # Resolve thinking: explicit `think` wins; else default per alias.
    if think is None:
        think = alias not in _THINKING_OFF_BY_DEFAULT
    if not think:
        # DashScope (Qwen) honours enable_thinking; Gemini honours a 0 budget.
        # LiteLLM forwards unknown keys to the provider; drop_params strips any
        # a given backend rejects, so this is safe to send broadly.
        if alias.startswith("qwen"):
            body["extra_body"] = {"enable_thinking": False}
        elif alias.startswith("gemini"):
            body["reasoning_effort"] = "none"

    headers = {"Authorization": "Bearer " + os.environ.get("LITELLM_MASTER_KEY", "")}
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{_LITELLM_BASE}/v1/chat/completions",
                                  json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return ToolResult(status="error", result=None,
                          error=f"HTTP {e.response.status_code}: {e.response.text[:500]}",
                          latency_ms=int((time.monotonic() - start) * 1000))
    except Exception as e:
        return ToolResult(status="error", result=None,
                          error=f"{type(e).__name__}: {e}",
                          latency_ms=int((time.monotonic() - start) * 1000))

    # Extract ONLY the final content — never the reasoning/thinking blocks.
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    usage = data.get("usage", {})
    cached = 0
    ptd = usage.get("prompt_tokens_details")
    if isinstance(ptd, dict):
        cached = ptd.get("cached_tokens", 0)

    return ToolResult(
        status="ok",
        result=content,
        tokens_used={
            "model": model,
            "prompt": usage.get("prompt_tokens", 0),
            "completion": usage.get("completion_tokens", 0),
            "cached": cached,
        },
        latency_ms=int((time.monotonic() - start) * 1000),
    )


class CallCloudLLM(Tool):
    name = "llm.call"
    description = (
        "Delegate a self-contained task to a cloud LLM. Pick `model` by "
        "cost/capability; default to the cheapest tier that can do the job. "
        "Pass a complete, standalone task — no conversation history is shared."
    )
    parameters = {
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "enum": ["haiku", "gemini_flash", "qwen_flash",
                         "claude", "qwen_plus",
                         "opus", "qwen_max",
                         "qwen_coder", "gemini_pro"],
                "description": (
                    "cheap/fast: haiku, gemini_flash, qwen_flash. "
                    "workhorse reasoning/writing: claude, qwen_plus. "
                    "frontier (costly, use sparingly): opus, qwen_max. "
                    "code: qwen_coder. long-context: gemini_pro."),
            },
            "task": {
                "type": "string",
                "description": "What to do. Specific and self-contained.",
            },
            "payload": {
                "type": "string",
                "description": "Optional content to act on (text to summarize, code, etc.).",
            },
            "system": {
                "type": "string",
                "description": "Optional system prompt override.",
            },
            "format": {
                "type": "string",
                "enum": ["text", "json"],
                "description": "Output format. 'json' requests a JSON object.",
            },
            "think": {
                "type": "boolean",
                "description": (
                    "Force the model's thinking/reasoning on or off. Omit to use "
                    "the per-model default (off for fast/code tiers, on otherwise). "
                    "Turn on for hard reasoning; off to save tokens/latency."),
            },
        },
        "required": ["model", "task"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        return await _call_via_litellm(
            args["model"], args["task"], args.get("payload"),
            args.get("system"), args.get("format") == "json",
            args.get("think"), ctx,
        )
```

</details>

### Stop 7: `tools/web/search_fetch.py` (~210 lines)

A realistic example of a tool with parsing, a multi-backend fallback chain, and
error handling. `WebSearch` tries **Tavily** (if `TAVILY_API_KEY` is set) →
SearxNG (if configured) → DuckDuckGo HTML, each falling through on failure.
`WebFetch` tries Tavily `/extract` → a direct GET that strips scripts/styles/tags
and truncates.

The fallback chain is the lesson here: the tool **works with no API key at all**
(DDG is always available), SearxNG upgrades it to a free local backend, and
Tavily upgrades it again to LLM-oriented search with pre-extracted content — but
each is optional and degrades gracefully. Tavily is a *paid external API* and its
calls leave your network, which is why it's opt-in via the key rather than a hard
dependency; this is a deliberate cost/privacy choice, the same one the privacy
gate enforces elsewhere.

Notice the defensive truncation in `WebFetch`: a fetch that returns 200KB of
HTML gets capped to 20KB of text by default. Without this, a single fetch
could blow up the orchestrator's context budget on the next iteration.

**Build it yourself — `tools/web/search_fetch.py`**

*Goal:* two tools (`web.search`, `web.fetch`) that show what a *realistic* tool
looks like: external I/O, a **priority chain of backends with graceful
fallback**, HTML parsing, and disciplined truncation.

*What to write:*

1. **`WebSearch`** (`web.search`) — params `query` and bounded `max_results`.
   Backend priority: if `TAVILY_API_KEY` is set and `tools.web.tavily_enabled`,
   POST to Tavily's `/search` (bearer auth) and map its `{title,url,content}` to
   your `{title,url,snippet}` shape; on *any* exception fall through. Else read
   `ctx.config["tools"]["web"]["search_endpoint"]`: if set, query a self-hosted
   SearxNG JSON API; if null, scrape DuckDuckGo's HTML endpoint (unwrapping the
   `//duckduckgo.com/l/?uddg=` redirect). Each field length-capped.
2. **`WebFetch`** (`web.fetch`) — params `url` and `max_chars`. Reject non-http
   schemes. If Tavily is available, POST the URL to `/extract` and use its
   `raw_content`; on failure fall through to a direct GET that strips
   `<script>`/`<style>` then all tags, unescapes, collapses whitespace. Take the
   **min** of requested and configured caps. Return
   `{url, content[:cap], truncated, original_length, via}`.

*Watch out for:* the fallback `try/except` must catch broadly and *fall through*
rather than error — the whole point is that a Tavily outage (or missing key)
silently degrades to the free backends instead of breaking the run. The
truncation cap is still the single most important line for runaway-context
protection (2.5). Send a real `User-Agent` or some endpoints 403. The Tavily key
is read from the env (`os.environ`), not from `runtime.yaml` — secrets stay out
of config files. (And per copyright hygiene: this fetches text for the model to
reason over, not to reproduce verbatim.)

<details>
<summary>Reference implementation — <code>tools/web/search_fetch.py</code></summary>

```python
"""Web search and fetch tools.

Search backend priority:
  1. Tavily API (if TAVILY_API_KEY is set) — LLM-oriented search with extracted
     snippets. Paid external API; leaves your network.
  2. Self-hosted SearxNG (if tools.web.search_endpoint configured) — free, local.
  3. DuckDuckGo HTML (always available) — free fallback, no key.

Fetch backend priority:
  1. Tavily /extract (if TAVILY_API_KEY set) — clean content extraction.
  2. Direct httpx GET + tag-strip (always available) — free fallback.

The fallback chain means the tools work with no API key at all; Tavily simply
upgrades quality when its key is present. Config lives under tools.web in
runtime.yaml; the key is read from the TAVILY_API_KEY env var.
"""

from __future__ import annotations

import os
import re
import html as html_lib
from urllib.parse import urlparse

import httpx

from runtime.tool_base import Tool, ToolContext, ToolResult


_DDG_URL = "https://html.duckduckgo.com/html/"
_TAVILY_SEARCH = "https://api.tavily.com/search"
_TAVILY_EXTRACT = "https://api.tavily.com/extract"
_UA = "Mozilla/5.0 (X11; Linux x86_64) Orchestrator/1.0"


def _tavily_key() -> str | None:
    key = os.environ.get("TAVILY_API_KEY")
    return key or None


class WebSearch(Tool):
    name = "web.search"
    description = (
        "Search the web for current information. Returns a list of "
        "{title, url, snippet} results. Use for facts that may have changed, "
        "recent events, or anything requiring up-to-date sources."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query, 1-6 words ideal."},
            "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        query = args["query"]
        n = min(int(args.get("max_results", 5)), 10)
        cfg = ctx.config.get("tools", {}).get("web", {})
        endpoint = cfg.get("search_endpoint")

        # Priority: Tavily -> SearxNG -> DDG. Each falls through on failure so a
        # transient outage in one backend degrades instead of erroring the run.
        if _tavily_key() and cfg.get("tavily_enabled", True):
            try:
                return ToolResult(status="ok",
                                  result=await self._search_tavily(query, n, cfg))
            except Exception as e:
                # Fall through to the free backends rather than failing.
                pass

        try:
            if endpoint:
                results = await self._search_searxng(endpoint, query, n)
            else:
                results = await self._search_ddg(query, n)
            return ToolResult(status="ok", result=results)
        except Exception as e:
            return ToolResult(status="error", result=None,
                              error=f"all search backends failed: {type(e).__name__}: {e}")

    async def _search_tavily(self, query: str, n: int, cfg: dict) -> list[dict]:
        body = {
            "query": query,
            "max_results": n,
            "search_depth": cfg.get("tavily_depth", "basic"),  # "basic" | "advanced"
            "include_answer": False,
        }
        headers = {"Authorization": f"Bearer {_tavily_key()}"}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(_TAVILY_SEARCH, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
        out = []
        for item in (data.get("results") or [])[:n]:
            out.append({
                "title": (item.get("title") or "")[:200],
                "url": item.get("url", ""),
                "snippet": (item.get("content") or "")[:300],
            })
        return out

    async def _search_searxng(self, endpoint: str, query: str, n: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": _UA}) as client:
            r = await client.get(endpoint, params={"q": query, "format": "json"})
            r.raise_for_status()
            data = r.json()
        out = []
        for item in (data.get("results") or [])[:n]:
            out.append({
                "title": item.get("title", "")[:200],
                "url": item.get("url", ""),
                "snippet": (item.get("content") or "")[:300],
            })
        return out

    async def _search_ddg(self, query: str, n: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": _UA},
                                     follow_redirects=True) as client:
            r = await client.post(_DDG_URL, data={"q": query}, timeout=15)
            r.raise_for_status()
            body = r.text
        out: list[dict] = []
        pattern = re.compile(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        for m in pattern.finditer(body):
            if len(out) >= n:
                break
            url = html_lib.unescape(m.group(1))
            if url.startswith("//duckduckgo.com/l/?uddg="):
                from urllib.parse import unquote, parse_qs
                qs = parse_qs(url.split("?", 1)[1])
                url = unquote(qs.get("uddg", [""])[0])
            title = re.sub(r"<[^>]+>", "", m.group(2))
            snippet = re.sub(r"<[^>]+>", "", m.group(3))
            out.append({
                "title": html_lib.unescape(title).strip()[:200],
                "url": url,
                "snippet": html_lib.unescape(snippet).strip()[:300],
            })
        return out


class WebFetch(Tool):
    name = "web.fetch"
    description = (
        "Fetch the text content of a URL. Returns plain-text extracted from HTML, "
        "truncated to a reasonable length. Use after web.search to read a specific page."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL (with https://)."},
            "max_chars": {"type": "integer", "default": 20000, "minimum": 500, "maximum": 100000},
        },
        "required": ["url"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        url = args["url"]
        max_chars = int(args.get("max_chars", 20000))
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ToolResult(status="error", result=None,
                              error=f"unsupported scheme: {parsed.scheme}")

        cfg = ctx.config.get("tools", {}).get("web", {})
        timeout = cfg.get("fetch_timeout_s", 15)
        cap = min(max_chars, cfg.get("max_content_chars", 50000))

        # Tavily /extract first (clean text), fall back to direct GET + strip.
        if _tavily_key() and cfg.get("tavily_enabled", True):
            try:
                text = await self._fetch_tavily(url)
                truncated = len(text) > cap
                return ToolResult(status="ok", result={
                    "url": url, "content": text[:cap],
                    "truncated": truncated, "original_length": len(text),
                    "via": "tavily",
                })
            except Exception:
                pass  # fall through to direct fetch

        try:
            text = await self._fetch_direct(url, timeout)
        except Exception as e:
            return ToolResult(status="error", result=None,
                              error=f"fetch failed: {type(e).__name__}: {e}")
        truncated = len(text) > cap
        return ToolResult(status="ok", result={
            "url": url, "content": text[:cap],
            "truncated": truncated, "original_length": len(text),
            "via": "direct",
        })

    async def _fetch_tavily(self, url: str) -> str:
        headers = {"Authorization": f"Bearer {_tavily_key()}"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(_TAVILY_EXTRACT, json={"urls": [url]}, headers=headers)
            r.raise_for_status()
            data = r.json()
        results = data.get("results") or []
        if not results:
            raise RuntimeError("tavily extract returned no content")
        # Tavily returns raw_content per URL.
        return results[0].get("raw_content") or ""

    async def _fetch_direct(self, url: str, timeout: int) -> str:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": _UA},
                                     follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            body = r.text
        body = re.sub(r"<script\b[^>]*>.*?</script>", " ", body,
                      flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<style\b[^>]*>.*?</style>", " ", body,
                      flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", body)
        text = html_lib.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text
```

</details>

### Stop 8: `tools/code/execute.py` (~120 lines)

Sandboxed Python execution via firejail. The `_build_cmd` method shows the
firejail flags — no network, read-only filesystem, memory cap, CPU cap. If
firejail isn't installed, falls back to plain subprocess and logs a warning.

This is a place to be paranoid. Read the firejail flags carefully and consider
whether you trust them. If running on a multi-user box, add more restrictions
(seccomp filters, user namespaces).

**Build it yourself — `tools/code/execute.py`**

*Goal:* the last starter tool — run a short Python snippet in a sandbox and
return stdout. This is where "tools can do real things" meets "real things are
dangerous," so the build is mostly about containment.

*What to write:*

1. **`CodeExecute`** (`code.execute`) — params `code` and bounded `timeout_s`.
   Description tells the model to `print()` its result and that there's no
   network and limited imports.
2. **Prepend a preamble** that trims `sys.path` of `/home` entries, then write
   the source to a temp file inside the configured workdir.
3. **`_build_cmd`** — if `sandbox == "firejail"` and `firejail` is on PATH,
   build a locked-down invocation: `--net=none`, `--private-tmp`,
   `--private-cwd`, `--read-only=/` with a single `--read-write=<workdir>`, and
   `--rlimit-as` / `--rlimit-cpu` caps. If firejail is missing, **log a loud
   warning and fall back to bare `python`** — acceptable for a single-user box,
   not for anything shared.
4. **Run via `asyncio.create_subprocess_exec`** wrapped in `asyncio.wait_for`
   for the timeout (kill on expiry). Capture stdout/stderr, tail them to
   sane lengths, and return ok/error with the exit code. Clean up the temp file
   in `finally`.

*Watch out for:* this is the highest-risk file in the project. The fallback
path runs *unsandboxed* code — that warning exists for a reason. Tail the output
(`out[-5000:]`) so a `while True: print(x)` can't flood the context. The
`allowed_imports` list in `runtime.yaml` is advisory documentation here, not a
hard import hook — if you want true import restriction, enforce it in the
preamble or a custom importer.

<details>
<summary>Reference implementation — <code>tools/code/execute.py</code></summary>

```python
"""Code execution tool.

Runs short Python snippets in a sandboxed subprocess. Default sandbox is firejail
(must be installed: `sudo pacman -S firejail`). Falls back to plain subprocess if
firejail is unavailable, but that's NOT recommended on a multi-user box.

The tool is intentionally limited — for heavy computation, build a dedicated
service. This is for things like math, JSON manipulation, regex tests, quick
unit-conversion, parsing.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import textwrap
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult


_PREAMBLE = """\
# Auto-injected preamble: bounded imports, no network, no fs writes outside cwd
import sys, os
sys.path = [p for p in sys.path if p and not p.startswith('/home')]
"""


class CodeExecute(Tool):
    name = "code.execute"
    description = (
        "Execute a short Python snippet and return stdout. Sandboxed; "
        "no network, limited imports, 30s timeout. Use for math, JSON manipulation, "
        "regex tests, quick computations. Output must be printed to stdout."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source. Use print() to return values.",
            },
            "timeout_s": {
                "type": "integer", "default": 30, "minimum": 1, "maximum": 60,
            },
        },
        "required": ["code"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        code = args["code"]
        cfg = ctx.config.get("tools", {}).get("code", {})
        timeout = min(int(args.get("timeout_s", cfg.get("timeout_s", 30))), 60)
        sandbox = cfg.get("sandbox", "firejail")
        workdir = Path(cfg.get("workdir", "/tmp/orch-sandbox"))
        workdir.mkdir(parents=True, exist_ok=True)

        full_source = _PREAMBLE + "\n" + textwrap.dedent(code)

        with tempfile.NamedTemporaryFile(
                "w", suffix=".py", dir=workdir, delete=False) as f:
            f.write(full_source)
            script = f.name

        try:
            cmd = self._build_cmd(script, sandbox, workdir)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(status="error", result=None,
                                  error=f"execution timeout after {timeout}s")

            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                return ToolResult(status="error", result={
                    "stdout": out[-2000:], "stderr": err[-2000:],
                    "exit_code": proc.returncode,
                }, error=f"exit code {proc.returncode}")

            return ToolResult(status="ok", result={
                "stdout": out[-5000:],
                "stderr": err[-1000:] if err else "",
                "exit_code": 0,
            })
        finally:
            try:
                Path(script).unlink()
            except OSError:
                pass

    def _build_cmd(self, script: str, sandbox: str | None, workdir: Path) -> list[str]:
        python = "python"
        if sandbox == "firejail" and shutil.which("firejail"):
            return [
                "firejail",
                "--quiet",
                "--noprofile",
                "--net=none",                       # no network
                "--private-tmp",
                f"--private-cwd={workdir}",
                "--read-only=/",
                f"--read-write={workdir}",
                "--rlimit-as=1073741824",           # 1 GB virtual memory cap
                "--rlimit-cpu=60",
                python, script,
            ]
        # Fallback: no sandbox. Logged but allowed for dev convenience.
        import logging
        logging.warning("firejail unavailable — running code.execute WITHOUT sandbox")
        return [python, script]
```

</details>

---

## 8. Operating llama.cpp

The orchestrator treats `llama-server` as a black-box LLM behind an HTTP endpoint
— but running that box well is a meaningful skill on its own. This section is
the practical user guide: what the moving parts are, how to pick flags, how to
hot-swap models without taking the system down, and what to do when something
breaks.

### 8.1. What llama.cpp actually is

llama.cpp started as a CPU-only inference engine for LLaMA models and grew into
the de facto local inference runtime. It's a single C++ codebase that produces
several binaries — `llama-server`, `llama-cli`, `llama-quantize`, `llama-imatrix`,
`llama-bench`, and many more. They all share the same core: a tensor library
called GGML, hardware backends (CPU, CUDA, Vulkan, Metal, ROCm/HIP, SYCL), and
a model loader that reads GGUF files.

For our purposes, only `llama-server` matters. It exposes an OpenAI-compatible
HTTP API and serves one model at a time per process. Everything else is either
preparation (quantizing, building an imatrix) or operations (swapping models,
monitoring).

### 8.2. Anatomy of the launch command

Look at `scripts/start-llama.sh` and you'll see a lot of flags. Each one is
controlling a real trade-off. The ones worth understanding deeply:

**`--model` / `-m`** — path to a GGUF file. That's it. No remote downloads, no
caching layers. Whatever's on disk is what loads.

**`--ctx-size` / `-c`** — context window size in tokens. This is the maximum
the model will see in any single request. The KV cache scales linearly with
this value, so doubling context roughly doubles VRAM usage for the cache
(separate from model weights). Default in this project is 32k; bump it for
long-document work, drop it if you're VRAM-constrained.

**`--n-gpu-layers` / `-ngl`** — how many transformer layers to offload to GPU.
Set to `999` (or any number bigger than the model has) to put everything on
GPU. Set lower to leave layers on CPU — useful if the model doesn't fit but
you accept slower inference. With dual R9700s and a 30B model, this is always
"all layers."

**`--split-mode`** — how to split a model across multiple GPUs.
- `layer` (default for us): each GPU gets some whole layers. No inter-GPU
  traffic during decode. Best for bandwidth-limited cards like the R9700.
- `row`: each layer is split across GPUs. More parallelism on big batches,
  but lots of NVLink/PCIe traffic. Hurts decode on AMD cards.
- `none`: ignore extra GPUs.

**`--tensor-split`** — proportions for the split. `1,1` = equal across two
GPUs. `3,1` = 75/25 split (useful if one GPU is being shared with another
workload).

**`--cache-type-k` / `--cache-type-v`** — quantize the KV cache itself. `q8_0`
(8-bit) is essentially free in quality but halves cache VRAM vs the default
FP16. `q4_0` saves more at modest quality cost. Worth turning on for any
context >8k.

**`--flash-attn`** — enable Flash Attention. Faster prefill, lower memory
footprint for long contexts. Should always be on for modern GPUs that support
it. Some older architectures don't; if you see crashes, drop it.

**`--batch-size` / `--ubatch-size`** — prefill batching. `--batch-size` is
how many tokens are processed per call; `--ubatch-size` (micro-batch) is
how many are processed in parallel within that. Larger batches = faster
prefill on long prompts, more VRAM. 2048/512 is a reasonable default.

**`--threads`** — CPU threads. Only matters for layers on CPU; if everything's
on GPU, leave at a modest number (8–16) for the sampler and tokenizer.

**`--jinja`** — use the chat template embedded in the GGUF file, rendered via
Jinja2. Modern instruction-tuned models include a template; without `--jinja`,
llama-server falls back to a generic format that won't match what the model
was trained on. **Always use `--jinja` for chat/tool-call workloads.**

**`--chat-template-file`** — override the embedded template with an external
file. Useful when you're debugging tool-call rendering or the model card
recommends a different template than the GGUF bakes in.

**`--metrics`** — exposes Prometheus-compatible metrics at `/metrics`. Useful
once you start caring about request rates and latencies.

**`--port` / `--host`** — bind address. The orchestrator uses `127.0.0.1:8090` (clear of serve.sh on 8080); the point is to keep it
local-only. Never bind to `0.0.0.0` without auth in front of it; llama-server
has no built-in authentication.

**Build it yourself — `scripts/start-llama.sh` and `config/qwen3-tools.jinja`**

*Goal:* the launcher that turns the flags above into a real command, and the
chat template that makes Qwen3 emit tool calls in the format the loop parses.
Together they are the `--model … --jinja --chat-template-file …` line made
concrete.

*What to write:*

1. **`scripts/start-llama.sh`** — a `set -euo pipefail` bash script with every
   tunable as an overridable `${VAR:-default}` (binary path, model path, port,
   host, ctx, gpu-layers). It **reuses the existing ROCm `llama-server`** from
   `build_tools.sh` — default `LLAMA_BIN` to
   `/srv/llama/llama.cpp-rocm/build/bin/llama-server`; do not compile a new one.
   Export the gfx1201/RDNA4 env (`GPU_MAX_HW_QUEUES=1`) and pin to GPU0 by
   default via `HIP_VISIBLE_DEVICES`/`ROCR_VISIBLE_DEVICES` (a ~15-20GB
   orchestrator model fits on one R9700, leaving GPU1 for `serve.sh`/`sd`); add
   an `ORCH_GPU=""` escape hatch that drops the pin and adds `--split-mode layer
   --tensor-split 1,1` for a >30GB model needing both cards. End with `exec
   llama-server` and the flag set from 8.2: `--cache-type-k/v q8_0`,
   `--flash-attn on`, `--jinja`, `--chat-template-file …/qwen3-tools.jinja`,
   `--metrics`, `--log-disable`. Guard with an `[[ ! -x "$LLAMA_BIN" ]]` check
   that points the user at `build_tools.sh`. The systemd unit from Section 5
   runs exactly this.
2. **`config/qwen3-tools.jinja`** — the chat template. It must: when `tools` are
   present, emit a `# Tools` system block listing each tool as JSON inside
   `<tools></tools>` and instruct the model to wrap calls in
   `<tool_call>{...}</tool_call>`; render user/assistant/system turns with the
   `<|im_start|>`/`<|im_end|>` ChatML markers; serialize assistant `tool_calls`
   back into `<tool_call>` blocks; and wrap `role:"tool"` messages in
   `<tool_response>`. Mirror the official Qwen3-30B-A3B-Instruct template — this
   is one place to copy, not invent.

*Watch out for:* the template is the contract between the model's output and
`loop.py`'s parser. If tool calls come back as invalid JSON, this file (or a
llama.cpp version mismatch) is the first suspect — exactly the row in the 6.5
troubleshooting table. Re-verify it against the model card whenever you rebuild
llama.cpp. `--log-disable` keeps journald quiet; drop it temporarily when
debugging prompt rendering.

<details>
<summary>Reference implementation — <code>scripts/start-llama.sh</code></summary>

```bash
#!/usr/bin/env bash
# Launch the orchestrator's llama-server (the local "brain") on :8090.
#
# This REUSES your existing ROCm build — it does not compile anything. The
# binary is produced and refreshed by /srv/llama/build_tools.sh. After a
# `pacman -Syu` that touches ROCm/kernel/Mesa, re-run build_tools.sh as usual;
# this script picks up the new binary on the next `orchstop && orchstart`.
#
# A ~15-20GB orchestrator model (e.g. Qwen3-30B-A3B at IQ4_XS) fits on a single
# R9700, so we pin to GPU0 by default and leave GPU1 free for serve.sh / sd.

set -euo pipefail

# Reuse the ROCm llama-server from build_tools.sh (NOT an orchestrator-owned build).
LLAMA_BIN="${LLAMA_BIN:-/srv/llama/llama.cpp-rocm/build/bin/llama-server}"
MODEL_PATH="${MODEL_PATH:-/srv/models/Qwen3-30B-A3B-Instruct-IQ4_XS.gguf}"
PORT="${PORT:-8090}"
HOST="${HOST:-127.0.0.1}"

# Context size — Qwen3 supports up to 128k, but each token costs KV memory.
# 32k is a sensible default for orchestration; bump for long-context work.
CTX="${CTX:-32768}"
N_GPU_LAYERS="${N_GPU_LAYERS:-999}"   # all layers on GPU

# --- RDNA4 / gfx1201 environment (mirrors build_tools.sh + runner.sh) ---
# gfx1201 can stay pegged at 100% util after idle under HIP; this mitigates it.
export GPU_MAX_HW_QUEUES="${GPU_MAX_HW_QUEUES:-1}"
# Pin the orchestrator model to GPU0; serve.sh/sd-serve.sh can use GPU1.
# Set ORCH_GPU="" to make BOTH GPUs visible (needed only for a >30GB model).
ORCH_GPU="${ORCH_GPU:-0}"
if [[ -n "$ORCH_GPU" ]]; then
    export HIP_VISIBLE_DEVICES="$ORCH_GPU"
    export ROCR_VISIBLE_DEVICES="$ORCH_GPU"
fi

if [[ ! -x "$LLAMA_BIN" ]]; then
    echo "Error: llama-server not found at $LLAMA_BIN" >&2
    echo "Build it with: /srv/llama/build_tools.sh llama rocm" >&2
    exit 1
fi

# Multi-GPU flags only when both GPUs are visible (ORCH_GPU="").
MULTI_GPU_FLAGS=()
if [[ -z "$ORCH_GPU" ]]; then
    MULTI_GPU_FLAGS=(--split-mode layer --tensor-split 1,1)
fi

exec "$LLAMA_BIN" \
    --model "$MODEL_PATH" \
    --host "$HOST" \
    --port "$PORT" \
    --ctx-size "$CTX" \
    --n-gpu-layers "$N_GPU_LAYERS" \
    "${MULTI_GPU_FLAGS[@]}" \
    --batch-size 2048 \
    --ubatch-size 512 \
    --cache-type-k q8_0 \
    --cache-type-v q8_0 \
    --flash-attn on \
    --jinja \
    --chat-template-file /srv/orchestrator/config/qwen3-tools.jinja \
    --metrics \
    --log-disable
```

</details>

<details>
<summary>Reference implementation — <code>config/qwen3-tools.jinja</code></summary>

```jinja
{# Qwen3 chat template with native tool-call support.
   Mirrors the official template from Qwen/Qwen3-30B-A3B-Instruct on HuggingFace.
   If you upgrade your llama.cpp build, double-check this matches the model card.
#}
{%- if tools %}
    {{- '<|im_start|>system\n' }}
    {%- if messages[0].role == 'system' %}
        {{- messages[0].content + '\n\n' }}
    {%- endif %}
    {{- "# Tools\n\nYou may call one or more functions to assist with the user query.\n\nYou are provided with function signatures within <tools></tools> XML tags:\n<tools>" }}
    {%- for tool in tools %}
        {{- "\n" }}
        {{- tool | tojson }}
    {%- endfor %}
    {{- "\n</tools>\n\nFor each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n<tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call><|im_end|>\n" }}
{%- else %}
    {%- if messages[0].role == 'system' %}
        {{- '<|im_start|>system\n' + messages[0].content + '<|im_end|>\n' }}
    {%- endif %}
{%- endif %}
{%- for message in messages %}
    {%- if (message.role == "user") or (message.role == "system" and not loop.first) %}
        {{- '<|im_start|>' + message.role + '\n' + message.content + '<|im_end|>\n' }}
    {%- elif message.role == "assistant" %}
        {{- '<|im_start|>' + message.role }}
        {%- if message.content %}
            {{- '\n' + message.content }}
        {%- endif %}
        {%- for tool_call in (message.tool_calls or []) %}
            {%- if tool_call.function is defined %}
                {%- set tool_call = tool_call.function %}
            {%- endif %}
            {{- '\n<tool_call>\n{"name": "' }}
            {{- tool_call.name }}
            {{- '", "arguments": ' }}
            {%- if tool_call.arguments is string %}
                {{- tool_call.arguments }}
            {%- else %}
                {{- tool_call.arguments | tojson }}
            {%- endif %}
            {{- '}\n</tool_call>' }}
        {%- endfor %}
        {{- '<|im_end|>\n' }}
    {%- elif message.role == "tool" %}
        {%- if loop.previtem and loop.previtem.role != "tool" %}
            {{- '<|im_start|>user\n' }}
        {%- endif %}
        {{- '<tool_response>\n' + message.content + '\n</tool_response>\n' }}
        {%- if not loop.last and messages[loop.index].role != "tool" %}
            {{- '<|im_end|>\n' }}
        {%- elif loop.last %}
            {{- '<|im_end|>\n' }}
        {%- endif %}
    {%- endif %}
{%- endfor %}
{%- if add_generation_prompt %}
    {{- '<|im_start|>assistant\n' }}
{%- endif %}
```

</details>

### 8.3. Picking a quantization

GGUF files come in many quantization levels. The naming is alphabet soup —
here's the practical cheat sheet for picking one. **Section 9 explains how
quantization actually works under the hood** if you want the full theory.

- **F16**: full half-precision. ~2 bytes/parameter. The reference quality.
- **Q8_0**: 8-bit, ~1 byte/param. Indistinguishable from F16 in most uses.
- **Q6_K**: 6-bit, ~0.81 byte/param. Near-FP16 quality; worth it if it fits.
- **Q5_K_M**: 5-bit, ~0.7 byte/param. Very good quality, decent compression.
- **Q4_K_M**: 4-bit, ~0.6 byte/param. The community's default sweet spot.
- **IQ4_XS**: 4-bit with importance-matrix calibration, ~0.55 byte/param.
  Slightly smaller than Q4_K_M, matches or beats it on quality when the
  imatrix was generated well.
- **Q3_K_M / IQ3_M**: 3-bit. Noticeable quality drop; use only when desperate.
- **Q2_K, IQ2_XS**: 2-bit. Only worth it for 70B+ models that wouldn't fit otherwise.

The shorthand to remember:

| Model size | If you have VRAM for it | Bare-minimum quality |
|------------|------------------------|----------------------|
| 7B–13B     | Q6_K or Q8_0           | Q5_K_M               |
| 20B–34B    | Q5_K_M or Q6_K         | IQ4_XS / Q4_K_M      |
| 70B        | Q4_K_M                 | IQ3_M                |
| 100B+ MoE  | IQ4_XS                 | IQ3_M                |

For 30B-A3B (your default), Q4_K_M and IQ4_XS both fit comfortably on a
single R9700 with room for KV cache. IQ4_XS is a hair smaller; pick either.

**Where to get GGUFs**: HuggingFace. Look for uploaders who include `imatrix`
calibration (mradermacher, bartowski, unsloth all do this reliably). The
filename usually encodes the quant: `Qwen3-30B-A3B-Instruct-IQ4_XS.gguf`.

**Verify the SHA-256**: GGUF files are big and download corruption is real.
Most uploaders publish hashes alongside; `sha256sum` against them before you
load.

### 8.4. Knowing how much VRAM you'll use

A useful mental model:

```
total_vram ≈ model_weights + kv_cache + activations + overhead

model_weights = parameters × bytes_per_param
                30B × 0.55 bytes (IQ4_XS) ≈ 16.5 GB

kv_cache      = ctx_size × layers × hidden × 2 (K and V) × bytes_per_kv_elem
                For Qwen3-30B-A3B with 32k context, q8_0 KV cache: ~6 GB

activations   = ~1–2 GB depending on batch size

overhead      = ~500 MB
```

For a 30B-A3B model at IQ4_XS with 32k context and q8_0 KV cache, plan on
~25 GB across both R9700s — comfortable. Push context to 128k and KV cache
grows to ~24 GB on its own; the model still fits, but you've burned the
headroom.

`nvidia-smi` doesn't work on AMD; use `rocm-smi` or `radeontop` (the latter
is in the Arch repos).

### 8.5. Switching models on a running system

This is the question you actually asked. The naive answer is "restart the
service with a different `--model` flag" — and for development that's fine.
But there are three better options.

> **Port note:** the examples below use `8080`/`8081`/… as illustrative
> sequential ports. On *this* box those are already taken (`serve.sh`→8080,
> `sd-serve.sh`→8081), and the orchestrator's own llama-server runs on **8090**.
> If you adopt a multi-model scheme, pick a free block (e.g. 8090–8095) and set
> LiteLLM's `api_base` to match. The patterns are what matter, not the numbers.

#### Option A: Multiple systemd services on different ports

The simplest pattern: define one service per model, each on its own port.

```
/etc/systemd/user/llama-qwen30.service  → port 8080
/etc/systemd/user/llama-qwen32.service  → port 8081
/etc/systemd/user/llama-llama70.service → port 8082
```

Each service has its own `EnvironmentFile` with `MODEL_PATH=...` and `PORT=...`.
Only start the ones you want active. Add them all to your LiteLLM config under
different `model_name` entries pointing at different `api_base` URLs.

**Pros**: zero new tools, fully transparent, can run multiple models concurrently
if VRAM allows. **Cons**: manual; no automatic swap when you ask for a model
that isn't loaded.

#### Option B: llama-server's built-in router mode (`--models`)

Recent llama-server versions accept multiple `--models` arguments and route
requests to whichever model matches the `model` field in the request. Roughly:

```bash
llama-server \
    --models qwen30:/srv/models/Qwen3-30B-A3B-IQ4_XS.gguf \
    --models qwen32:/srv/models/Qwen3-32B-Q5_K_M.gguf \
    --port 8080
```

A request with `"model": "qwen30"` routes to the first; `"model": "qwen32"` to
the second. The server loads/unloads as needed.

**Pros**: built-in, one port, one process. **Cons**: less mature than the
external proxy; configuration is per-command-line so adding a model means
restarting; resource isolation between models is limited.

#### Option C: llama-swap (recommended for serious use)

[llama-swap](https://github.com/mostlygeek/llama-swap) is a Go proxy that sits
in front of one or more llama-server instances. Request a model by name, and
it transparently starts the right backend if it's not already running,
swapping out an existing one if VRAM is tight.

A `config.yaml` looks like:

```yaml
models:
  qwen30:
    cmd: |
      /srv/llama/llama.cpp-rocm/build/bin/llama-server
      --port ${PORT}
      -m /srv/models/Qwen3-30B-A3B-Instruct-IQ4_XS.gguf
      --ctx-size 32768 --n-gpu-layers 999
      --split-mode layer --tensor-split 1,1
      --flash-attn --jinja --cache-type-k q8_0 --cache-type-v q8_0

  qwen32:
    cmd: |
      /srv/llama/llama.cpp-rocm/build/bin/llama-server
      --port ${PORT}
      -m /srv/models/Qwen3-32B-Q5_K_M.gguf
      --ctx-size 16384 --n-gpu-layers 999
      --split-mode layer --tensor-split 1,1
      --flash-attn --jinja

  llama70:
    cmd: |
      /srv/llama/llama.cpp-rocm/build/bin/llama-server
      --port ${PORT}
      -m /srv/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf
      --ctx-size 8192 --n-gpu-layers 999
      --split-mode layer --tensor-split 1,1
      --flash-attn --jinja

groups:
  heavy:
    swap: true       # only one of these may be loaded at a time
    members: [qwen32, llama70]
  light:
    swap: false      # may run concurrently
    members: [qwen30]
```

Run llama-swap on port 8080 (the orchestrator's expected endpoint), let it
spawn llama-server processes on internal ports as needed. Idle models get
unloaded after a configurable TTL.

**Pros**: transparent to clients, process-level isolation (a model crash
doesn't take others down), groups handle VRAM constraints, works with vLLM
and other backends too. **Cons**: another process to install and configure.

For this learning project, I'd suggest staying with **Option A** until you
have at least three models you swap between regularly. The complexity of
llama-swap isn't worth it until you feel the pain it solves.

### 8.6. Running multiple models concurrently

If you want a fast small model alongside the orchestrator (e.g. for embedding
generation in phase 5, or as a "cheap local LLM" tool), the cleanest pattern
is two systemd services on different ports + different GPUs:

```
llama-orchestrator.service  → GPU 0, port 8090, Qwen3-30B-A3B
llama-embed.service         → GPU 1, port 8091, BGE-M3 (or similar)
```

Pin each to a specific GPU with `HIP_VISIBLE_DEVICES=0` / `=1` (the AMD/ROCm
equivalent of `CUDA_VISIBLE_DEVICES`; for Vulkan use `GGML_VK_VISIBLE_DEVICES`).
This is exactly the `ORCH_GPU` knob `start-llama.sh` already exposes.

Alternatively, use llama-swap with a `swap: false` group containing both,
provided VRAM allows.

### 8.7. Performance tuning

Once you have a model running, you'll want to know how fast it is and where
the bottlenecks are.

**`llama-bench`** is shipped alongside `llama-server`:

```bash
/srv/llama/llama.cpp-rocm/build/bin/llama-bench \
    -m /srv/models/Qwen3-30B-A3B-Instruct-IQ4_XS.gguf \
    -ngl 999 -fa 1 -ctk q8_0 -ctv q8_0 \
    -p 512,1024 -n 128
```

Reports tokens-per-second for prefill (`-p`) and decode (`-n`) at different
context sizes. Compare different flag combinations to see what matters.

**Real-world numbers to expect on dual R9700, Vulkan**:
- Qwen3-30B-A3B IQ4_XS: ~120 tok/s decode, ~1500 tok/s prefill (single card)
- Qwen3-32B Q5_K_M: ~25–35 tok/s decode (dense model, bandwidth-bound)
- Llama 3.3 70B Q4_K_M: ~12–18 tok/s decode (split across both cards)

If you're getting much less, the usual culprits are:
- `--flash-attn` not enabled
- KV cache not quantized (drop to q8_0 or q4_0)
- Wrong `--n-gpu-layers` (you want all layers on GPU)
- ROCm vs Vulkan backend choice — for RDNA4 in 2026, Vulkan is consistently
  competitive or faster

### 8.8. When llama-server breaks

A diagnostic checklist when something's wrong:

**Server won't start**
1. `journalctl --user -u llama-orchestrator.service -e` — look for the actual error.
2. Verify the model path exists and is readable.
3. Verify `vulkaninfo --summary` shows both R9700s. If not, fix the system first.
4. Check VRAM: `rocm-smi` should show free memory roughly matching `total - 1GB`
   before the model loads.

**Server starts but `/v1/models` returns nothing**
- Model file is corrupted. Re-download and verify SHA-256.
- GGUF is for a different llama.cpp version. Look for "unsupported GGUF version"
  in logs; either upgrade llama.cpp or get a re-quantized file.

**Inference produces garbage / random tokens**
- Wrong chat template. Drop `--chat-template-file` and let `--jinja` use the
  embedded one, or download the template from the model card.
- Quantization too aggressive (Q2/Q3 on a small model). Try a higher quant.
- KV cache corruption from a crash. Restart the server.

**Slow first request after restart**
- Normal. The first request prefills the system prompt into the KV cache
  (~30 seconds for 30B on dual R9700 via Vulkan). Subsequent requests reuse it.

**Tool-call format is wrong**
- The model isn't trained for tool calls. Check the model card.
- Or: `--jinja` isn't set, so the chat template isn't applying tool schemas correctly.
- Or: the embedded template predates the tool-calling format. Override with
  `--chat-template-file` and a known-good template (the project ships one in
  `config/qwen3-tools.jinja`).

**Server OOMs and crashes mid-request**
- Context grew larger than what fits given the KV cache size. Either lower
  `--ctx-size` or quantize the KV cache more aggressively.
- Another process grabbed VRAM (gaming, browser, second model). Use `rocm-smi`
  to see what's resident.

**Throughput drops over time**
- Memory fragmentation. Restart the server — usually solves it.
- Or background system load (file indexer, backup). Check `htop`.

### 8.9. Things to read when you want to go deeper

The llama.cpp repo has good documentation buried in subdirectories:
- `tools/server/README.md` — every flag and HTTP endpoint.
- `tools/quantize/README.md` — how to quantize your own models from F16.
- `docs/build.md` — backend-specific build options.
- `examples/server/public/` — the built-in chat UI is at `http://127.0.0.1:8080`
  if you ever want a quick test interface.

For the GGUF format itself:
- The format spec is in `ggml/docs/gguf.md` in the repo.
- HuggingFace's GGUF viewer (huggingface.co/docs/hub/en/gguf) gives a nice
  visual of what's inside a file.

---

## 9. Quantization: how it works and how to choose

Section 8.3 told you what quantization levels to pick. This section tells you
*why* — because once you understand the underlying mechanics, the alphabet
soup (Q4_K_M, IQ4_XS, Q5_K_S) stops being arbitrary and starts making sense.

This is the longest "theory" section in the guide because quantization is the
single technique that determines whether a model runs on your hardware at all.
Worth understanding properly.

### 9.1. The starting point: FP16 weights

A trained LLM stores its parameters as floating-point numbers. The default
storage format is **FP16** (half-precision, 16 bits per number) or **BF16**
(bfloat16, a different 16-bit layout favored during training). Either way,
each parameter takes 2 bytes.

A 30B parameter model in FP16: 30 × 10⁹ × 2 = **60 GB**.

That doesn't fit on any consumer GPU. Even your dual R9700s (64 GB total)
have basically no headroom for KV cache, activations, or anything else. To
make models like this usable on local hardware, you have to shrink the
weights themselves. That's what quantization does.

### 9.2. The naive idea: just use fewer bits

The simplest possible quantization scheme: instead of storing each weight as
a 16-bit float, store it as an 8-bit integer. You've cut storage in half.

The problem: integers can't directly represent the kinds of values a neural
net actually has. A weight might be `0.0237` or `-1.84` or `0.0000031`. You
can't put those in a byte (which only holds integers 0–255 or –128 to 127).

So instead of storing the values directly, you store **scaled integers**:

```
For each block of weights, store:
  - one floating-point "scale" factor
  - the actual values as small integers

To reconstruct: weight = integer × scale
```

This is called **affine quantization**. The scale lets you cover a wide range
of values with a small integer. A block where the largest absolute weight is
1.7 would have `scale = 1.7 / 127`, and the integer `127` would mean `1.7`,
`64` would mean `0.85`, etc.

The accuracy you lose depends on:
1. **How many bits the integer has** (more bits = finer steps).
2. **How big the block is** (smaller blocks = each scale fits its values better).
3. **How the scale is calibrated** (one global scale is worse than per-block scales).

Every "quantization scheme" you'll see is just a different combination of
these three choices, plus tricks to compress the scales themselves.

### 9.3. The original llama.cpp formats: Q4_0, Q4_1, Q8_0, etc.

The first quantization formats llama.cpp shipped were what we now call
"legacy" formats. Their names encode the scheme:

- **Q**: quantized
- **digit**: bits per weight
- **suffix `_0`**: symmetric quantization (one scale per block, zero stays at zero)
- **suffix `_1`**: asymmetric quantization (one scale + one offset per block)

So:
- **Q4_0**: 4-bit weights, symmetric, blocks of 32 weights with one FP16 scale
- **Q4_1**: 4-bit weights, asymmetric (scale + offset), blocks of 32
- **Q8_0**: 8-bit weights, symmetric — the highest quality of the legacy formats

These work, but they're crude. Every weight in the model uses the same number
of bits, every block has the same structure. There's no awareness that some
weights matter more than others.

You'll still see Q8_0 used a lot because at 8 bits the quality loss is
negligible. The other legacy formats are deprecated — anywhere they'd appear
you should use a K-quant instead.

### 9.4. K-quants: the modern foundation

In mid-2023, llama.cpp introduced **K-quants**. The "K" doesn't stand for
anything in particular — it's just the version-2 naming. K-quants improved
on the legacy formats in three ways:

**1. Block-of-blocks structure.** Instead of one scale per small block,
K-quants use a two-level hierarchy:
- A "super-block" of 256 weights has one FP16 scale and one FP16 offset.
- Within that, sub-blocks of 16 weights each have their own tiny scale (also
  quantized — typically 6 bits) that's applied on top of the super-block scale.

This nested structure lets the quantization adapt to local variation within
the super-block without spending a full FP16 scale per sub-block. Better
fidelity, similar storage.

**2. Mixed precision across layers.** Different parts of the model are
quantized to different levels. The K-quant scheme picks more bits for
**attention** layers (where errors compound across the sequence) and fewer
bits for **feed-forward** layers (which are more redundant). The variant
suffixes `_S` / `_M` / `_L` (small/medium/large) encode different mixtures.

**3. Importance-aware allocation.** Even within a quant level, K-quants spend
more bits on weights with bigger magnitudes (which contribute more to the
output) and fewer bits on near-zero weights.

The K-quants you'll actually use:

| Format | Bits/weight | Notes |
|--------|-------------|-------|
| Q2_K | ~2.6 | Extreme compression, big quality loss. Use only for 70B+. |
| Q3_K_S | ~3.2 | Smaller variant of Q3_K. Noticeable quality drop. |
| Q3_K_M | ~3.4 | Medium variant. Acceptable for 70B+ in a pinch. |
| Q3_K_L | ~3.5 | Large variant (keeps more layers at higher precision). |
| Q4_K_S | ~4.3 | Smaller Q4. Saves a bit of space vs Q4_K_M for similar quality. |
| **Q4_K_M** | **~4.5** | **The community default. Excellent quality/size trade-off.** |
| Q5_K_S | ~5.2 | Smaller Q5. Hard to distinguish from Q5_K_M in practice. |
| Q5_K_M | ~5.4 | Slightly better than Q4_K_M, ~20% larger. |
| Q6_K | ~6.6 | Near-FP16 quality. Worth it if you have the VRAM. |
| Q8_0 | 8.5 | (Legacy format kept for compatibility.) Indistinguishable from FP16. |

The `_M` variants are almost always the right pick within a quant level — the
`_S` saves ~5% size for a perceptible quality cost, and `_L` adds ~5% size
for a marginal gain.

### 9.5. I-quants: importance-matrix calibration

In late 2023, llama.cpp introduced a second family: **I-quants** (the "I" is
for *importance matrix*). These take K-quants further by using a calibration
dataset to determine *which weights matter most*, then allocating bits
accordingly.

Here's how the calibration works:

1. You feed a representative text corpus through the FP16 model.
2. For each weight, you measure how much the model's output activations
   depend on it (technically, the second derivative — the importance matrix
   or "imatrix").
3. The quantization process uses this imatrix to weight the rounding decisions:
   important weights get rounded carefully; less-important weights can be
   rounded coarsely.

Result: at the same average bits-per-weight, an I-quant beats a K-quant on
benchmark accuracy, particularly at very low bit counts (3 bits and below)
where the K-quant scheme starts to break down.

The I-quants you'll see:

| Format | Bits/weight | Notes |
|--------|-------------|-------|
| IQ2_XXS | ~2.1 | Extreme. Only for 70B+ where nothing else fits. |
| IQ2_XS | ~2.3 | Slightly better than IQ2_XXS. Still extreme. |
| IQ2_S / IQ2_M | ~2.5–2.7 | Best 2-bit options. Usable on big models. |
| IQ3_XXS | ~3.1 | Smaller than Q3_K_S, similar quality. |
| IQ3_S / IQ3_M | ~3.4–3.7 | Solid 3-bit choices. |
| IQ4_XS | ~4.3 | **Slightly smaller than Q4_K_M, often equal or better quality.** |
| IQ4_NL | ~4.5 | Non-linear 4-bit. Sometimes better than IQ4_XS, especially on AMD/ROCm. |

**The cost of I-quants**: they're slower to compute. Specifically, **prompt
processing is meaningfully slower** because the dequantization step has more
branches. Decode speed is closer to K-quants but still slightly behind on
most hardware.

On modern GPUs with good kernel support (CUDA, Vulkan on RDNA4) the speed
penalty is small — a few percent on prompt, ~5% on decode. On older or
weaker hardware, the gap widens. For your R9700s on Vulkan, IQ4_XS is
competitive with Q4_K_M.

### 9.6. Decoding the file names

Once you understand the components, the file names tell a complete story.

Take `Qwen3-30B-A3B-Instruct-IQ4_XS.gguf`:

- `Qwen3-30B-A3B-Instruct`: the source model (30B MoE with 3B active).
- `IQ4_XS`: I-quant, 4 bits per weight (average ~4.3 bpw), "XS" variant of the
  4-bit family (smallest, ~2% bits saved vs the standard variant).
- `.gguf`: the file format llama.cpp uses.

Or `Llama-3.3-70B-Instruct-Q4_K_M.gguf`:

- `Llama-3.3-70B-Instruct`: Meta's 70B model.
- `Q4_K_M`: K-quant, 4 bits, "M" (medium) variant — the standard 4-bit choice
  with balanced layer-wise precision.

Sometimes you'll see additional suffixes:
- `-iMat` or `-imatrix`: the GGUF was quantized with an imatrix calibration
  even if it's a K-quant. Strictly better than non-calibrated at the same level.
- `-i1`: same thing, different uploader convention (bartowski uses this).
- `-128k`: the model's context length has been extended.

When in doubt, check the model card on HuggingFace — uploaders document what
they did. The convention isn't fully standardized.

### 9.7. How much quality do you actually lose?

The numbers vary by model and benchmark, but the rough shape is consistent.
**Perplexity** (PPL) is the standard metric — lower is better; it measures
how surprised the model is by held-out text.

For a typical 7B model, with FP16 as baseline:

| Quant | PPL increase vs FP16 | Subjective quality |
|-------|---------------------|--------------------|
| Q8_0 | +0.01% | Indistinguishable |
| Q6_K | +0.1% | Indistinguishable |
| Q5_K_M | +0.3% | Indistinguishable in casual use |
| Q4_K_M | +1% | Slight degradation in edge cases |
| IQ4_XS | +1% | Similar to Q4_K_M |
| Q3_K_M | +5% | Noticeable but usable |
| IQ3_M | +4% | Similar to Q3_K_M |
| Q2_K | +25% | Significantly degraded |
| IQ2_M | +15% | Usable for big models, terrible for small ones |

Two patterns from years of community benchmarking:

**Bigger models tolerate more quantization.** A 70B model at Q2_K can still
beat a 7B model at FP16 on most tasks — the parameter count buffers the
quantization noise. A 7B model at Q2_K is barely functional.

**The interesting quality cliff is between 4 and 3 bits.** Going from FP16
to Q4_K_M loses about 1% perplexity — invisible in most use. Going from
Q4_K_M to Q3_K_M loses another 4% — that you'll feel. Below 3 bits, you're
in compromise territory.

For most users on most hardware, the sweet spot is **Q4_K_M or IQ4_XS**.
You're trading ~1% quality for 4× compression vs FP16. That's an extraordinary
deal.

### 9.8. The right quant for your situation

A practical decision tree:

```
Does the model fit in VRAM at Q6_K with headroom for KV cache?
  YES → Use Q6_K or Q5_K_M. Near-perfect quality.
  NO  → continue
       │
       ▼
Does it fit at Q4_K_M with headroom?
  YES → Use Q4_K_M or IQ4_XS. The standard choice.
  NO  → continue
       │
       ▼
Is the model 30B+ parameters?
  YES → IQ3_M or Q3_K_M is acceptable. Use this.
  NO  → Pick a smaller model. Don't go below 3 bits on <30B.
       │
       ▼
Truly out of options on a 30B+?
  YES → IQ2_M is the floor. Quality will be visibly degraded.
```

For your dual-R9700 box (64 GB VRAM total):

| Model | Recommended quant | File size | Total VRAM (with 32k context, q8 KV) |
|-------|-------------------|-----------|--------------------------------------|
| Qwen3-30B-A3B | IQ4_XS or Q4_K_M | ~16 GB | ~23 GB |
| Qwen3-32B dense | Q5_K_M | ~22 GB | ~30 GB |
| Llama 3.3 70B | Q4_K_M | ~42 GB | ~52 GB |
| Mistral Large 123B | IQ3_M | ~52 GB | ~62 GB (tight) |

The Mistral Large entry is right at your VRAM limit — you'd have to drop
context size or accept fragility. Not recommended for daily use.

### 9.9. Quantizing your own GGUFs (and when to bother)

Most of the time, you'll download pre-quantized GGUFs from HuggingFace. The
established uploaders — **bartowski**, **mradermacher**, **unsloth**,
**TheBloke** (less active now) — produce well-calibrated quants with proper
imatrix datasets. There's rarely a reason to do it yourself.

But sometimes you want to, especially if:
- You're working with a model that nobody has quantized yet.
- You want a specific quant level the uploader didn't ship.
- You want to use your own domain-specific calibration data.

The workflow:

```bash
# 1. Download the FP16 source from HuggingFace
huggingface-cli download <repo> --local-dir /srv/models/source

# 2. Convert HF format to GGUF FP16
cd /srv/llama/llama.cpp-rocm
python convert_hf_to_gguf.py /srv/models/source \
    --outfile /srv/models/source-f16.gguf --outtype f16

# 3. (Optional but recommended) Generate an imatrix
./build/bin/llama-imatrix \
    -m /srv/models/source-f16.gguf \
    -f calibration-data.txt \
    -o /srv/models/imatrix.dat

# 4. Quantize using the imatrix
./build/bin/llama-quantize \
    --imatrix /srv/models/imatrix.dat \
    /srv/models/source-f16.gguf \
    /srv/models/source-iq4_xs.gguf \
    IQ4_XS
```

The **calibration data** is critical for I-quants. The standard choice is
~10 MB of diverse text — Wikipedia samples, code, conversation transcripts.
The llama.cpp repo includes example calibration files; for general use,
they're fine. For domain-specialized models you might use domain-specific
text, but the gains are marginal compared to the convenience cost.

### 9.10. The KV cache is also quantized

One detail that surprises people: when section 8 mentioned `--cache-type-k q8_0`
and `--cache-type-v q8_0`, that was quantizing the **KV cache** (the
per-conversation attention state), not the model weights. Same concept,
different target.

The KV cache scales linearly with context length and can dominate VRAM at
long contexts. Quantizing it from FP16 to Q8 halves its footprint with
negligible quality loss. Q4 halves it again with a small quality cost.

For a 32k context on Qwen3-30B-A3B:
- FP16 KV cache: ~12 GB
- Q8 KV cache: ~6 GB
- Q4 KV cache: ~3 GB

Always use Q8 KV cache. Use Q4 KV cache only if you're VRAM-constrained at
your target context length, and accept a small quality hit on long-context
recall.

### 9.11. What to remember

A condensed take-home:

1. **Quantization compresses weights from FP16 to fewer bits per number,
   using scale factors to preserve range.** That's the entire mechanism.
   Everything else is engineering details.

2. **K-quants are the modern baseline.** They use a two-level block structure
   and mixed precision across layers.

3. **I-quants add calibration-aware bit allocation.** They're slightly
   smaller at the same quality, slightly slower to compute.

4. **The community sweet spot is Q4_K_M or IQ4_XS** for most models on
   consumer hardware. ~1% quality loss for ~4× compression.

5. **Bigger models tolerate more quantization.** 70B at Q3 > 7B at FP16 on
   most benchmarks.

6. **Don't go below 3 bits on models under 30B.** Quality falls off a cliff.

7. **The KV cache is separately quantizable** via `--cache-type-k/v`. Use Q8
   by default.

8. **Trusted uploaders** (bartowski, mradermacher, unsloth) save you from
   ever needing to quantize yourself. Look for "imatrix" in the filename.

---

## 10. Embeddings and rerankers (the RAG stack)

RAG — Retrieval-Augmented Generation — is what lets an LLM answer questions
grounded in your documents. The orchestrator picks `rag.search`, the tool
returns relevant chunks, the model writes an answer using those chunks. We
implement this in phase 5; this section explains the moving parts so you
know what you're committing to.

There are two models worth running locally for a good RAG stack, separate
from the orchestrator LLM itself:

1. An **embedder** that turns text into vectors for similarity search.
2. A **reranker** that reorders an embedder's top-N candidates for precision.

Both are small (0.3B–4B), fast, and run alongside the orchestrator on the
same box. They're the difference between a RAG system that mostly works and
one that genuinely surfaces the right chunk.

### 10.1. Why two models, not one

The naive RAG pipeline is one model: embed everything, embed the query, return
the top-k by cosine similarity. This works, but has a structural problem.

Embedders are **bi-encoders**: they encode the query and each document
*independently* into fixed vectors, then compare with a cheap similarity
metric (cosine, dot product). That independence is what makes them fast — you
can pre-compute every document's vector once, store it, and at query time
only the query needs encoding. Millions of documents searched in milliseconds.

But the independence is also why they're imprecise. A bi-encoder never sees
the query and a candidate document *together*; it just sees two vectors and
asks "are these close?" Fine for "find me the rough neighborhood." Bad for
"is this specific document the best match?"

Rerankers are **cross-encoders**: they take the query and one candidate
document as a *joint input* and output a single relevance score. They can
attend across both texts — see that the query asks about X and the document
discusses Y, even when the surface vectors look similar. Much more accurate.
Much slower: you can't pre-compute anything, and every (query, candidate)
pair is a full model forward pass.

The standard pipeline composes them:

```
1. Embedder retrieves top-100 candidates from vector DB    (~10ms, cheap)
2. Reranker scores all 100 candidate pairs                  (~200ms, expensive)
3. Return the top 5–10 by reranker score                    (free)
4. Pass those to the LLM as context                         (the actual answer)
```

You get the embedder's speed at scale plus the reranker's accuracy at the top
of the funnel. Without a reranker, you're sending mediocre chunks to your most
expensive model.

### 10.2. Picking an embedder: Qwen3-Embedding

The Qwen3 team released a dedicated embedding family in 2025 that sits at the
top of the MTEB multilingual leaderboard. Three sizes, Apache-2.0:

| Model | Params | Embed dim | Context | VRAM (FP16) |
|-------|--------|-----------|---------|-------------|
| Qwen3-Embedding-0.6B | 0.6B | 1024 | 32k | ~1.5 GB |
| Qwen3-Embedding-4B | 4B | 2560 | 32k | ~8 GB |
| Qwen3-Embedding-8B | 8B | 4096 | 32k | ~16 GB |

A few properties that matter for a local RAG stack:

**100+ languages, including strong German.** If your corpus is anything other
than pure English, multilingual matters — and BGE-M3 (the previous community
default) is being matched or beaten on multilingual tasks by Qwen3-Embedding
even at the 0.6B size.

**32k context.** Most older embedders are capped at 512 or 8k tokens, forcing
aggressive chunking. Qwen3-Embedding can swallow long passages whole and
return one good vector for them.

**Instruction-tunable.** You can prepend a task description like
"`Given a code search query, retrieve relevant code snippets`" or
"`Given a German legal query, retrieve relevant regulations`" and the
embedder shifts toward that domain. Free quality lift when your task fits.

**MRL (Matryoshka)-trained.** You can truncate the output vector to a smaller
dimension (e.g. 768 instead of 1024) for storage savings, and quality
degrades gracefully rather than falling off a cliff. Useful when your vector
DB billing is by storage.

**Recommendation for this project**: **Qwen3-Embedding-0.6B**. It hits MTEB
scores within a couple of points of the 8B model at a fraction of the cost,
loads in <2 GB, and runs ~10× faster than the 8B. The 4B is worth it if you
have specialised needs (code search, very long documents) and quality
benchmarks back up the upgrade for your corpus. The 8B is rarely worth it
locally; the marginal gain over 4B is small and the latency hurts.

### 10.3. Picking a reranker

You named `bge-reranker-v2-m3` and that's a perfectly defensible choice — it's
the practical baseline that most production RAG systems still use. But the
Qwen3-Reranker series ships alongside Qwen3-Embedding and is now genuinely
better on benchmarks.

From the Qwen3 paper's own numbers (MTEB-R / multilingual retrieval):

| Reranker | Params | MTEB-R | MMTEB-R |
|----------|--------|--------|---------|
| BGE-reranker-v2-m3 | 0.6B | 57.0 | 58.4 |
| Jina-multilingual-reranker-v2 | 0.3B | 58.2 | 63.7 |
| Qwen3-Reranker-0.6B | 0.6B | 65.8 | 66.4 |
| Qwen3-Reranker-4B | 4B | 69.8 | 72.7 |
| Qwen3-Reranker-8B | 8B | 69.0 | 72.9 |

**Two important caveats** before you blindly pick Qwen3-Reranker-8B:

1. **Latency cost is real.** Qwen3-Reranker uses a causal LM architecture
   (yes/no logit on a query-document pair) which requires autoregressive
   decoding overhead. Independent benchmarks suggest Qwen3-Reranker-4B can
   take >1 second per query reranking 100 candidates on a single GPU.
   `bge-reranker-v2-m3` does the same workload in ~50–100ms because it's a
   single forward pass through a SequenceClassification head.

2. **The retriever sets the ceiling.** No reranker can surface a chunk that
   the embedder missed. If your top-100 candidates are bad, even Qwen3-Reranker-8B
   produces mediocre top-10s. Spend effort on chunking and embedder choice
   before optimizing the reranker.

**Recommendation for this project**:

- **Start with `bge-reranker-v2-m3`** for the first phase 5 build. It's
  battle-tested, fast (~50ms reranks for 100 candidates), the API is
  well-supported by every serving framework, and the quality is good enough
  that you'll be evaluating the rest of the pipeline rather than the reranker.

- **Upgrade to `Qwen3-Reranker-0.6B`** once your pipeline works end-to-end
  and you want a quality bump. Same size as BGE, ~8 points better on
  benchmarks, similar serving infrastructure. This is the right long-term
  default.

- **Only consider Qwen3-Reranker-4B+** if you have a specific task where
  benchmarks show it matters for *your* data, and you can afford the latency.
  For general RAG, 0.6B is the sweet spot.

### 10.4. The serving layer: text-embeddings-inference (TEI)

You could run the embedder and reranker via llama.cpp (they have GGUF
quantizations available), but the better tool for the job is HuggingFace's
**text-embeddings-inference (TEI)**. It's purpose-built for this — handles
batching, dynamic shape inference, OpenAI-compatible `/v1/embeddings` and
`/rerank` endpoints, far better throughput than llama.cpp for embed/rerank
workloads specifically.

Running it via podman (matches your existing service patterns):

```bash
# Embedder on port 8081, GPU 1
podman run -d --name embed-server \
    --device=/dev/dri --group-add keep-groups \
    --gpus device=1 \
    -p 127.0.0.1:8081:80 \
    -v /srv/models/hf-cache:/data \
    ghcr.io/huggingface/text-embeddings-inference:1.7.2 \
    --model-id Qwen/Qwen3-Embedding-0.6B \
    --dtype float16

# Reranker on port 8082, GPU 1 (shares with embedder, both are small)
podman run -d --name rerank-server \
    --device=/dev/dri --group-add keep-groups \
    --gpus device=1 \
    -p 127.0.0.1:8082:80 \
    -v /srv/models/hf-cache:/data \
    ghcr.io/huggingface/text-embeddings-inference:1.7.2 \
    --model-id BAAI/bge-reranker-v2-m3 \
    --dtype float16
```

Both can share a single R9700 — they're under 4 GB combined, and the
orchestrator LLM gets the other card to itself. Or pin both to GPU 0 alongside
the orchestrator (the 30B-A3B model uses ~17 GB; an R9700 has 32 GB, plenty of
room for two more 1–2 GB models).

Note: TEI's AMD support is currently via the ROCm image variant rather than
CUDA. If the Vulkan path is your preference for the orchestrator LLM, you'll
end up with both Vulkan (llama.cpp) and ROCm (TEI) on the same box. They
coexist fine — different libraries, different processes.

### 10.5. What an embed call looks like

The TEI server speaks an OpenAI-compatible API. From the orchestrator's
perspective (and the future `rag.search` tool), it's two HTTP calls:

```bash
# 1. Embed a query
curl -s http://127.0.0.1:8081/v1/embeddings \
    -H 'Content-Type: application/json' \
    -d '{"model": "Qwen/Qwen3-Embedding-0.6B",
         "input": ["Instruct: Given a search query, retrieve relevant passages\nQuery: How does Flash Attention work?"]}'
# Returns: {"data": [{"embedding": [0.012, -0.043, ...], "index": 0}]}

# 2. Rerank candidates
curl -s http://127.0.0.1:8082/rerank \
    -H 'Content-Type: application/json' \
    -d '{"query": "How does Flash Attention work?",
         "texts": ["Flash Attention is an algorithm that...",
                   "GPU memory hierarchy includes L1/L2 caches...",
                   "The transformer architecture uses self-attention..."]}'
# Returns: [{"index": 0, "score": 0.987},
#           {"index": 2, "score": 0.412},
#           {"index": 1, "score": 0.156}]
```

Notice the **`Instruct:` prefix** in the embedding query. Qwen3-Embedding is
instruction-tuned: prepending a task description shifts the model toward the
relevant domain. You don't need this on the document side during ingestion —
only on the query side at search time. The ingestion path just embeds the
chunk text directly.

This is one of those small details that's easy to miss and costs measurable
retrieval quality if you skip it.

### 10.6. The full RAG pipeline at a glance

To put the pieces together, here's what phase 5's `rag.search(query)` will do
end-to-end:

```
┌────────────────────────────────────────────────────────────────┐
│  Orchestrator calls rag.search("how does flash attention work")│
└─────────────────────────────┬──────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│ Step 1: Embed the query                                        │
│   POST :8081/v1/embeddings with "Instruct: ...\nQuery: ..."    │
│   → 1024-dim vector                                            │
│   ~10ms                                                        │
└─────────────────────────────┬──────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│ Step 2: Vector search in Qdrant                                │
│   POST :6333/collections/<col>/points/search                   │
│   → top-100 candidates with metadata                           │
│   ~5ms for <1M vectors                                         │
└─────────────────────────────┬──────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│ Step 3: Rerank candidates                                      │
│   POST :8082/rerank with query + 100 candidate texts           │
│   → reordered list with relevance scores                       │
│   ~50ms (bge-reranker) or ~200–1000ms (qwen3-reranker)         │
└─────────────────────────────┬──────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│ Step 4: Return top-K to orchestrator                           │
│   Default K=5; orchestrator can override                       │
│   Each result: {text, score, metadata}                         │
└────────────────────────────────────────────────────────────────┘
```

Total latency: ~70ms with BGE reranker, ~250ms+ with Qwen3-Reranker-4B.
Either is fast enough that the orchestrator can call `rag.search` multiple
times per request without you noticing.

### 10.7. Chunking: where most RAG quality comes from

A point worth driving home before phase 5 lands: **embedder and reranker
choice matter less than chunking strategy.** Bad chunks make any retrieval
system bad. Good chunks make almost any retrieval system decent.

The principles in brief:

- **Chunk by semantic unit**, not character count. A section of a manual, a
  function in code, a paragraph of a blog post. Splitting mid-sentence is
  almost always a mistake.
- **Overlap chunks** by ~10–20% so context isn't lost at boundaries.
- **Include metadata in the embedding when it helps disambiguate.** A chunk
  from a "Migration guide" header section should embed with that context, so
  searches for "migration" find it even if the chunk body doesn't say the word.
- **Size matters less than you think for modern embedders.** Qwen3-Embedding
  handles 32k tokens; you can have big chunks and let the embedder figure it
  out. The old "always 512 tokens" rule was a constraint of older embedders,
  not a universal best practice.
- **Different content types want different strategies.** Code wants AST-aware
  splitting (treesitter). Markdown wants header-based. PDFs want page-aware.

Phase 5 will implement this with separate chunkers per content type and
metadata propagation through Qdrant. None of it requires changes to the
embedder or reranker — they sit downstream of chunking and ingest whatever
you give them.

### 10.8. Evaluating a RAG system

You'll want a way to know whether your RAG is good. The honest answer is:
build a small evaluation set early.

A pragmatic minimum:
- 20–50 (query, expected-answer) pairs that you wrote by hand from your real
  corpus.
- For each query, label which chunk IDs in your corpus contain the answer
  (manual work; takes a couple of hours).
- Metrics: **recall@10** (did the right chunk appear in the top-10?) and
  **MRR** (mean reciprocal rank — how high up was it on average?).

Re-run this eval whenever you change the embedder, reranker, or chunking. A
single 50-row eval will catch more regressions than any amount of "looks good
to me" vibe-checking.

For a more rigorous approach later: **BEIR**-style evaluation against public
benchmarks, or **RAGAS** for end-to-end RAG quality (retrieval + generation).
These are worth setting up once you have a baseline working and want to
compare alternatives systematically.

---

## 11. Exercises to deepen understanding

These are graded roughly from easy to hard. Each one adds real capability.

### Exercise 1: Add a `time.now` tool

The simplest possible new tool. Create `tools/time/now.py`:

```python
from datetime import datetime, timezone
from runtime.tool_base import Tool, ToolContext, ToolResult

class TimeNow(Tool):
    name = "time.now"
    description = "Return the current UTC date and time."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args, ctx):
        return ToolResult(status="ok", result={
            "iso": datetime.now(timezone.utc).isoformat(),
            "unix": int(datetime.now(timezone.utc).timestamp()),
        })
```

Don't forget `tools/time/__init__.py`. Restart the llama service. Run
`orch "What's the current time?"` and watch it call your tool.

### Exercise 2: Parallel tool execution

Currently, when the model returns multiple tool calls in one turn, the loop
executes them sequentially. Modify `runtime/loop.py` to run independent calls
concurrently with `asyncio.gather`.

Hint: the model rarely emits parallel calls today, but Qwen3 sometimes does
for independent operations like "search for X *and* fetch Y". You'll need to
think about how budget accounting handles concurrent updates — `Budget` isn't
thread-safe right now.

### Exercise 3: Pre-summarize large tool results

Add a `max_result_chars` per-tool config. When a tool returns a result longer
than the cap, automatically have the local model produce a summary before
appending to messages. This is a real-world technique called
"context compression."

Trickier than it sounds: you need to preserve the *information* the next
iteration might need without paying for the full content. One approach: keep
the full result on disk, append a summary to messages, and add a
`tool.retrieve_full(result_id)` tool the model can call if it needs the
unabridged version.

### Exercise 4: A `notes` tool with persistent state

Add `tools/notes/`:
- `notes.append(text)` — write a line to `data/notes.md`.
- `notes.search(query)` — grep `data/notes.md` for a substring.

This is your first stateful tool. Use it like a scratchpad across runs.

Bonus: make `notes.search` use embeddings instead of grep. You'll have done
the prep work for phase 5 RAG without realizing it.

### Exercise 5: A second orchestrator model

Modify `runtime.yaml` to support multiple orchestrator profiles (e.g., a fast
profile using Qwen3-30B-A3B for routine tasks and a slow profile using
Qwen3-32B for hard ones). Add a CLI flag `--profile slow` to pick.

This forces you to think about where "the model" is hardcoded in the code.
(Spoiler: `AgentRuntime.model`, used in `_model_turn` and budget accounting.)

### Exercise 6: A retry tool

Sometimes a model produces a clearly wrong answer and you want to "re-roll."
Add `meta.retry()` — when called, the orchestrator discards its last assistant
message and tries again with a slightly higher temperature.

This is one of those "tools the model uses on itself" patterns. Think carefully
about loop-guard interaction.

### Exercise 7: Cost-aware tool selection

Add a `cost_hint_usd` field to every `Tool` class — an estimate of typical
cost-per-call. Modify the orchestrator system prompt to include these hints
("`llm.call` with model="opus" typically costs $0.05/call; model="haiku"
typically costs $0.001/call"). See if the model becomes more frugal.

### Exercise 8: Implement Level-2 sub-agents (preview)

Stub out `tools/meta/spawn.py`:

```python
class AgentSpawn(Tool):
    name = "agent.spawn"
    description = "Run a specialized sub-agent on a sub-task."
    ...
    async def execute(self, args, ctx):
        # Load YAML from agents/<name>.yaml
        # Build a new AgentRuntime with restricted tools
        # Run it with the sub-task
        # Return its final answer
```

This is the heart of phase 7. If you build it as an exercise first, the phase
7 version will be a polish pass rather than a new concept.

---

## Applied phases — a design-and-decisions map

Chapters 1–11 build the irreducible core, each with a full *Build it yourself*
listing. The chapters that follow — §§12–23 — cover everything added since. They
are larger and change faster, so rather than full reference listings they are a
**design-and-decisions map**: what each piece is for, the reasoning behind it, the
gotchas, and the file that is the real source of truth. Read them next to the code,
not instead of it.

Three things diverged from the original roadmap, and the *why* is the lesson. RAG
shipped on SQLite + numpy rather than Qdrant — the two-stage retrieve→rerank theory
of §10 still holds, only the storage backend got simpler (§13). MCP was deliberately
*not* built: when you write every tool natively, a bridge to other people's tools
hasn't yet earned its keep (§12). And a confirmation gate, a live event sink, and a
web console appeared — none in the plan, all driven by actually using the thing
(§14–§15). The machinery anticipated most of it: tools are still just files the
registry discovers, private namespaces are gated by construction (§3.4), and the
`requires_confirmation` flag that sat dormant on the `Tool` base class finally does
something (§14).

## 12. The capability tools

### 12.1 The capability tools

Phases 1–4 gave the orchestrator three ways to reach the world: cloud models
(`llm.call`), the web (`web.*`), and a sandboxed scratchpad (`code.execute`).
This set turns it from "talks to models" into "acts on the box." Every one is a
plugin auto-discovered by the registry; the private ones are gated by §3.4; the
mutating ones declare `requires_confirmation` (§14).

**Real compute — `job.*` (and why it is *not* `code.execute`).** This is the most
important distinction in the whole tool set. `code.execute` is a *safety
sandbox*: firejail, no network, no GPU, ~seconds, stdlib-only. Perfect for math
and JSON munging, useless for AI work. `job.*` is the opposite — real commands
with GPU access, no sandbox — and it is **detached**: `job.start` spawns the
process in a new session, writes one directory per job (`data/jobs/<id>/` with
`meta.json`, `stdout.log`, `exit_code`), and returns a `job_id` *immediately*.
You then poll `job.status` / `job.logs`, and `job.cancel` if needed. The reason
for the fire-and-poll shape is structural: a training run outlives the 300s
wall-clock budget and any sane tool timeout, so the loop must never block on it.
Detaching also means the job survives an orchestrator restart, and cancelling a
*run* (§15) leaves running jobs alone — by design. Marked private and
`requires_confirmation`. Sources your `rdna4-env.sh` so the RDNA4 workarounds
apply. *File:* `tools/job/runner.py`.

**Filesystem — `fs.*`** (the realized half of the old phase-6 plan). `fs.read`
(line-numbered, range-sliceable, byte-bounded), `fs.list` (glob + depth, skips
`.git`/`__pycache__`/`node_modules`/`.venv`), `fs.grep` (regex → `file:line`
hits), `fs.write` and `fs.edit` (a unique-match string replace — the safe way to
patch a file; it refuses on zero or multiple matches). Everything is confined to
`tools.fs.allowed_roots`; a path outside them is refused. Private; write/edit are
confirmation-gated. *File:* `tools/fs/ops.py`.

**Version control — `git.*`** `status` / `diff` / `log` / `show` / `add` /
`commit` / `branch`. Diffs and logs are bounded so they can't blow the context
window; ops are confined to `allowed_roots`. The namespace is **private** on
purpose — a diff of your code shouldn't auto-forward to a cloud model; set
`--share-private` for the run when you *want* Claude to read a diff. Writes are
confirmation-gated. *File:* `tools/git/status.py`.

**Telemetry — `gpu.status`** Wraps `rocm-smi --json` (with an `amd-smi` fallback
and the raw output always included, because field names drift across ROCm
releases). Per GPU: VRAM used/total, utilisation, temperature, power. The brain
should call it *before* launching a heavy `job.start` to confirm there's
headroom. Not private — GPU telemetry is fine to reason about anywhere. *File:*
`tools/gpu/status.py`.

**Persistence — `memory.*` and `kg.*` (the local "world model").** This is the
piece that answers the original "develop a local world model" goal, and the
distinction between the two matters. `memory.*` is free-form durable
notes/facts/decisions with FTS5 full-text search (`append` / `search` / `get` /
`list` / `delete`). `kg.*` is a structured knowledge graph — typed entities with
JSON attributes and directed relations you can traverse (`upsert_entity` /
`add_relation` / `query` / `neighbors` / `remove_relation`). Both live in one
SQLite file (`data/memory.db`). The line against RAG (§13) is sharp and worth
keeping: **RAG is read-mostly retrieval over documents you indexed; memory/kg are
facts the agent *maintains itself* and carries across runs.** That self-maintained
state is what turns a stateless loop into something that accumulates a model of
its world. Both private. *Files:* `tools/memory/store.py`, `tools/kg/graph.py`.

**Research & evaluation — `arxiv.*` and `eval.compare`.** `arxiv.search` /
`arxiv.get` hit the arXiv Atom API and return clean structured metadata (id,
title, authors, abstract, categories, PDF url) — prefer them over `web.search`
for ML/AI literature. `eval.compare` fires one prompt at N models concurrently
through LiteLLM and returns their outputs side by side with latency, tokens, and
cost — use it to pick a model or cross-check the local brain. Its one non-obvious
design point: it charges the run's `Budget` per sub-call (the loop's single
`tokens_used` envelope can't represent N models), so a comparison across pricey
models still counts against `max_cost_usd`. *Files:* `tools/arxiv/search.py`,
`tools/eval/compare.py`.

**Config and dependencies for this set:**

```yaml
# runtime.yaml — tools.*
  job:
    jobs_root: /srv/orchestrator/data/jobs
    default_cwd: /srv/orchestrator/data/work
    env_setup: /srv/llama/lib/rdna4-env.sh   # sourced before each job
    default_gpus: "0,1"
  fs:
    allowed_roots: [/srv/orchestrator, /srv/models, /home/<you>/projects]
  git:
    default_repo: /srv/orchestrator
    allowed_roots: [/srv, /home/<you>]
  arxiv:
    min_interval_s: 3        # courtesy throttle (arXiv asks <=1 req / 3s)

privacy:
  # job/git/memory/kg join the original rag/fs as private namespaces
  private_tool_namespaces: [rag, fs, job, git, memory, kg]
```

`numpy` joins `requirements.txt` (for `rag` cosine, §13). `eval.compare` reuses
the existing `costs` table — no new config.

### 12.2 The MCP decision: why we didn't build it (yet)

The old plan treated the MCP bridge as "the high-leverage part." Building the
rest changed that assessment, and the reversal is instructive. **MCP's entire
value is consuming tools *other people* built and exposed as MCP servers.** For
anything you write yourself as a `Tool` subclass, a bridge is strictly worse: an
extra process/HTTP hop, a session handshake per call, a heavy dependency tree
(`mcp` + starlette/uvicorn/pydantic-settings), and — the real cost — it sits
*outside* your in-process `ctx`/budget/privacy machinery. Native tools are
faster, simpler to debug, and fully integrated with the gates this part adds.

So the call: **build natively now; add the `mcp.*` bridge only when a concrete
external server appears that you'd rather consume than rewrite** (a maintained
browser-automation server, a Postgres server, a vendor's official endpoint). The
SDK was verified to work — stdio, SSE, and streamable-HTTP all import cleanly — so
it's a quick bridge the day it's warranted. The empty `tools/mcp/` package stays
as the placeholder. This is the §3.7 consolidation-vs-explicitness tension
resolved by a simpler rule: *don't add an abstraction layer until it earns its
keep.*

## 13. RAG: retrieval and management

### 13.1 RAG, as actually built (phase 5)

The plan (old §12) called for Qdrant in a container plus dedicated embedding and
reranker servers pinned to GPU1. What shipped is deliberately humbler: **SQLite
storing embeddings as float32 blobs, brute-force cosine in numpy.** Reasons: it's
consistent with every other store in the project (trace/memory/kg/chats), it
needs no server, and brute-force cosine is genuinely fine for a single-user corpus
up to tens of thousands of chunks. The §10 theory didn't change — two-stage
retrieve→rerank, the reranker earning its VRAM, chunking driving quality — only
the *backend* did.

Tools: `rag.index` (chunk + embed text or a file), `rag.search` (cosine top-k,
with an optional rerank pass), `rag.collections`, `rag.delete`. Embeddings come
from **any OpenAI-compatible `/v1/embeddings` endpoint** — point `tools.rag.embed_url`
at your Qwen3-Embedding server (or let it default to the LiteLLM proxy); the
optional `tools.rag.rerank_url` enables a Cohere/Jina-style rerank stage when set.
This is the project-wide principle in action: **the tool code is endpoint-agnostic —
the location lives in config, never baked into the tool.** That's also the upgrade
path: when a corpus outgrows brute-force cosine, swap the search internals for
Qdrant/HNSW and the tool contract and config stay put.

`rag` is private by construction (§3.4), so a retrieved chunk can't reach
`llm.call` without `--share-private` — the privacy design from 3.4 finally doing
real work, exactly as predicted. *File:* `tools/rag/store.py`.

```yaml
# runtime.yaml — tools.rag
  rag:
    db_path: /srv/orchestrator/data/rag.db
    embed_url: http://<embed-host>:<port>/v1/embeddings
    embed_model: qwen3-embedding
    chunk_size: 1200
    chunk_overlap: 150
    # rerank_url: http://<host>:<port>/rerank
    # rerank_model: bge-reranker-v2-m3
```

> The live embedding/rerank HTTP is the one part not exercised by the build's
> tests (the sandbox is offline); the chunking, storage, and cosine ranking were
> tested with a stub embedder. Verify the first real query against your endpoint.

### 13.2 Managing RAG from the web

The `rag.*` tools could index and search, but nothing let you *see* or *prune*
what had accumulated. The admin page grew a RAG panel backed by four endpoints
that open `rag.db` directly — the same read-it-yourself pattern as the log viewer,
no ORM: list collections with their source and chunk counts and the database's
on-disk size; drill into one collection's sources; delete a collection (optionally
just one `source` within it); and empty everything, with a follow-up `VACUUM` to
reclaim the file. The read is a `GET`; the destructive operations are `DELETE` /
`POST` under `/api/admin/*`, so the existing admin-only middleware gates them and
no new auth surface appears. A not-yet-created table is treated as an empty store
rather than a 500, so the panel works on a fresh box. *Files:* `web/server.py`
(`/api/admin/rag*`), `web/static/admin.html` (the RAG tab).

## 14. The confirmation gate and the event sink

### 14.1 The confirmation gate

`Tool.requires_confirmation` has existed since Stop 1 and done nothing. This phase
makes it real. The split is clean: the **loop decides whether to ask** (the
`confirmation.enabled` switch, the tool's flag, the per-run `auto_confirm`
bypass), and a **pluggable provider decides how to ask**. With no provider, the
built-in path prompts on a TTY (`approve? [y/N]`) and falls back to
`non_interactive: allow|deny` when there's no terminal (e.g. under systemd) — so
the CLI behaviour is unchanged and a service never hangs waiting on stdin. The web
provider (§15) asks in the browser instead.

A declined call doesn't crash the run — it becomes a `ToolResult` error the model
*sees*, so it can adapt ("the user declined; let me try another way") rather than
looping. Gated tools across the set: `job.start`, `job.cancel`, `git.add`,
`git.commit`, `git.branch`, `fs.write`, `fs.edit`, `rag.delete`, `memory.delete`,
`kg.remove_relation`. *Files:* `runtime/loop.py` (`_confirm`), `runtime/confirm.py`.

```yaml
# runtime.yaml — confirmation
confirmation:
  enabled: true
  non_interactive: allow     # no-TTY fallback (systemd): allow | deny
  web_timeout_s: 300         # how long a browser approval waits
  web_on_timeout: deny       # if nobody answers in time
```

### 14.2 The event sink: making the loop observable in real time

The trace DB (Stop 4) already recorded every step — but `run()` was a black box
that returned only its final dict, so there was no way to *watch* a run as it
happened. The fix is a single seam: `run()` takes an optional async `on_event`
sink, and one `emit()` closure now feeds **both** the trace and that sink, so the
two never diverge. Events are transport-neutral dicts — `run_start`,
`tool_selection`, `model_turn`, `tool_result`, `confirmation_request`,
`confirmation`, `run_finish` — each with a monotonic `seq`. Cancellation is
handled too (a clean `cancelled` finish).

The crucial discipline: **the loop never imports HTTP.** `runtime/events.py` is an
`EventBus` with per-`run_id` fan-out plus a small replay buffer (so a browser that
connects late, or reconnects via `Last-Event-ID`, still sees the steps it
missed). SSE is just one adapter draining the bus; a WebSocket adapter, a log
file, or Chainlit would drain the same bus without touching `loop.py`. *That seam*
— not the choice of SSE — is what keeps every UI door open. *Files:*
`runtime/events.py`, `runtime/loop.py`.

## 15. The web console

### 15.1 The web console (FastAPI + SSE)

Chatting with the orchestrator is the easy half; *seeing what it does* is the
real requirement, and once the event sink exists (§14) it's just a transport
problem over data that already flows.

**Why SSE, not WebSocket or a framework.** SSE is server→client streaming with
free auto-reconnect and `Last-Event-ID` resume — a perfect match for "watch the
loop." It can't carry messages upstream, but it doesn't need to: approvals and
cancels are occasional, discrete actions, so a plain `POST` that resolves a
`Future` the loop is awaiting is *cleaner* than a duplex socket. SSE is also the
transport the big providers use for token streaming, so it's forward-compatible.
FastAPI keeps the WebSocket door open for the day something genuinely needs duplex
(add an `@app.websocket()` route draining the same bus). Chainlit would've been
faster to a demo but hides exactly the plumbing this project exists to teach.

**Shape.** The orchestrator becomes a long-running service (vs the per-invocation
`orch` CLI). `POST /api/chat` starts a run as a background task and returns its
`run_id` at once; `GET /api/stream/{run_id}` is the SSE feed (keep-alive pings,
resume); `POST /api/approve/{run_id}` resolves a `WebConfirmationProvider`'s
pending `Future`; `POST /api/cancel/{run_id}` cancels the task (detached jobs from
§12 keep running, intentionally). The single static page is one centred column:
each response renders the streamed answer, a **footer line** beneath it
(`model · turns · tools · tokens · cost · time`, ticking live as `cost` events
arrive), and — only when the run involved reasoning or tool use — a collapsible
**one-liner** above the footer (e.g. `▸ thinking · 2 tools · 4 steps`) that
expands to the per-step timeline: the model's planning text, the tool calls,
each result with latency, and any confirmations. (An earlier revision put that
timeline in a fixed side panel; folding it into a per-response disclosure keeps
the transcript clean and scoped each run's activity to its own answer — the same
collapsible-"thinking" pattern chat UIs use.) Gated tools get inline Approve/Deny
buttons in the conversation flow. The token-by-token streaming that fills the
answer and the live footer is covered in §15.

**Deployment.** Bind `0.0.0.0` when a reverse proxy on another host must reach it,
and add `--proxy-headers --forwarded-allow-ips=<proxy-ip>` so client IP/scheme are
correct. Because it's then LAN-reachable and can drive `job.start`/`fs.write`/
`git.commit`, protect it: `ORCH_WEB_TOKEN` and/or a firewall rule pinning the port
to the proxy. On the proxy itself, disable buffering on `/api/stream/` (`nginx:
proxy_buffering off`, long read timeout) or events arrive in clumps. *Files:*
`web/server.py`, `web/static/index.html`, `systemd/orchestrator-web.service`,
`requirements-web.txt`.

### 15.2 Chat history

The requested semantics — *not auto-saved; mark "to be saved"; un-marking deletes
with a confirm* — map onto one clean idea: **presence in the store is the saved
flag.** There is no `saved` boolean. Marking is an `upsert`; un-marking is a
`delete` (with the confirm modal in front). Ephemeral chats live only in the
browser and are never written. A saved chat auto-syncs as new turns arrive so it
stays current; the *initial* save is always an explicit click. Endpoints:
`GET/POST /api/chats`, `GET/PATCH/DELETE /api/chats/{id}`. *File:* `web/store.py`
(a `chats.db` SQLite store mirroring the others).

> **The single-turn caveat — now lifted (in chat).** The bare engine builds
> `[system, user]` fresh per message, so on its own it doesn't see earlier turns.
> Multi-turn was subsequently added (§15): the web chat replays prior turns so a
> saved chat *can* be continued with context. The CLI deliberately stays
> single-turn — see §15 for why.

### 15.3 Multi-turn and streaming (built — chat only)

Two features that landed after the web console: the chat now *remembers* within a
conversation, and it *streams* — tokens and cost — as the run unfolds.

**Multi-turn.** `run()` gained an optional `history` ( `[{role, content}, …]` ).
It builds `[system] + history + [user]`, so prior turns sit *after* the cacheable
system+tools prefix (cache hits preserved) and only `user`/`assistant` *text*
turns are replayed — never the internal tool-call transcript, which would bloat
context for no benefit. The browser owns the turn list and replays it on each
send, so a loaded saved chat (§15) is now genuinely continuable, not just a
record.

**Streaming.** Opt-in via a `stream` flag (the web path passes `True`; the CLI
leaves it `False`). A `_model_turn_streaming` consumes the proxy's SSE and
**reassembles the streamed content and tool-call deltas back into the exact
`{message, usage}` shape** the non-streaming path returns — so the rest of the
loop is unchanged — while asking for usage via `stream_options.include_usage` so
cost is still charged. New events flow over the same sink (§14): `token`
(`scope: "brain"` or `"llm.call"`) and `cost` (running `total_usd` /
`total_tokens` after each charge). In the UI the brain streams into a live answer
bubble, `llm.call` streams into the activity panel, and a header meter ticks up
the spend live. `cloud_models.py` got a matching streaming branch so delegated
cloud calls stream too. *Files:* `runtime/loop.py`, `runtime/tool_base.py`
(`ToolContext.on_token`/`stream`), `tools/llm/cloud_models.py`, `web/server.py`,
`web/static/index.html`.

> **Why this is chat-only, and that's correct.** Both are wired through the *web*
> path only; `orch` is untouched and still does a single non-streaming run per
> invocation. This isn't an oversight — multi-turn needs something to *hold* the
> prior turns between messages, and each `orch` call is a fresh process that exits
> when done, with no session to accumulate them. The browser **is** that session
> holder (it keeps `chat.turns` and replays them), which is why continuity lives
> there. A conversational terminal would be a separate REPL-style script keeping a
> turn list in a loop — not a change to `orch`. Keeping the CLI a clean one-shot
> also preserves its proven, non-streaming path.

### 15.4 Tidying and a mobile-friendly UI

Once the console had three columns — chats on the left, the conversation in the
middle, a tools panel on the right — it stopped fitting a phone. The fix was
deliberately CSS-first. Below 900px the two side panels become off-canvas drawers
toggled from the header (☰ and 🛠) with a backdrop scrim and Escape-to-close; at
desktop width the same two buttons collapse and expand the columns instead, and
both start collapsed so a fresh load is just the conversation. The tool list's
namespace groups also start collapsed. The admin page, which had grown into five
stacked sections, became a sticky tab bar — Status, Prompt, Users, RAG — so only
one shows at a time. The whole pass preserved every element id the JavaScript binds
to and changed no behaviour; it only added visible focus rings and honoured
`prefers-reduced-motion`. *Files:* `web/static/index.html`, `web/static/admin.html`.

## 16. Files in and out: uploads and delivery

For most of the build, files moved only one way: the agent could read what was
already on disk, but a web user could neither get a file *in* without dropping it
on the server by hand, nor get a produced file *out* except as text pasted into the
reply. Two features close that loop — uploads in, delivery out — and together with
the document skills they make a genuine round-trip possible.

### 16.1 Uploading files to a chat

A 📎 button sends each chosen file to `POST /api/upload` as a raw request body —
one request per file, so there's no multipart parser to depend on — stored per-user
under `uploads_dir/<owner>/<id>-<name>`. The chat request then carries only the
attachment **ids**, never a client-supplied path; the server re-derives the trusted
on-disk path inside the caller's own upload directory, with a basename-and-
containment check so a crafted id can't escape it. How an attachment reaches the
model depends on its kind: a text file is inlined into the message (bounded by a
character cap); an image is noted by path (the local brain is text-only, so the
note invites a vision/OCR step); any other binary is noted for a tool — or a skill
— to open. Images also render as thumbnails on the user's own chat bubble, served
by an owner-scoped `GET /api/upload/{id}`. One sharp edge worth recording:
classification is by extension, and a `.ics` calendar was treated as binary rather
than text, so its contents weren't inlined and a round-trip attempt went hunting
for the file the hard way — the fix is simply to widen the text-extension set.
*Files:* `web/server.py` (upload, attachment resolution, message augmentation),
`web/static/index.html`, config `web.uploads_dir` / `max_upload_mb`.

### 16.2 Delivering files back

The mirror image is `deliver.files`: a tool the agent calls with the absolute
path(s) of artifacts it produced. A single file is offered as-is; multiple files,
or any folder, are bundled into one `.tar.gz`, and repeated calls within a run
accumulate into the same deliverable. The bytes stage under `outputs_dir/<run_id>/`,
and the tool emits a live `output` event the console renders as a download chip on
that turn, served owner-scoped by `GET /api/output/{run_id}` with a real filename.
The design question that took the most thought was *lifetime*: a chat the user
never keeps shouldn't leave files on disk forever. So deliveries are **ephemeral
unless the chat is saved** — each manifest starts `saved: false`; saving the chat
flips its runs' outputs to kept; deleting the chat removes them; and a sweep (on
startup and throttled thereafter) clears any still-unsaved output past a TTL. The
window is generous enough to download in-session, but nothing orphaned survives.
Because the download chip rides in the turn's event log, reopening a saved chat
re-renders it. Mechanically this needed only two small additions to `ToolContext`
— `owner` (who to scope the output to) and `emit` (surface the live event) — plus
an `owner` argument on `run()`, mirroring the `spawn` seam from the sub-agents work.
*Files:* `runtime/outputs.py` (stage/bundle/manifest/sweep), `tools/deliver/files.py`,
`web/server.py` (`/api/output`, the save/delete hooks, the boot sweep),
`web/static/index.html`, config `web.outputs_dir` / `output_ttl_hours` /
`max_output_mb`. With uploads and the docx skill alongside it, the full loop finally
closes: upload a Word file, have the agent edit it, and get the edited file back as
a download.

## 17. Access control and two-factor auth

### 17.1 Login, per-user tool toggles, and the admin page

The console started as a single-user tool on a trusted LAN, protected by an
optional shared `ORCH_WEB_TOKEN`. Once it can drive `job.start`, `fs.write`, and
`git.commit`, "who is using it" stops being academic — so phase 10 adds real
accounts, per-user control over which tools a run may touch, and an admin surface
for the things you previously had to SSH in to do.

**Accounts without a new dependency.** Passwords are `pbkdf2-hmac-sha256` with a
per-user salt — stdlib `hashlib`, no `bcrypt`/`argon2` wheel to build under ROCm.
Sessions are **HMAC-signed cookies** rather than Starlette's `SessionMiddleware`,
which would have pulled in `itsdangerous`: a cookie carries `username|expiry`,
signed with a secret, and `read_session` verifies the signature
(`hmac.compare_digest`) and the expiry on every request. The secret comes from
`ORCH_SESSION_SECRET`, or is persisted once to `data/session.secret` so sessions
survive a restart (a per-process random key would silently log everyone out on
every redeploy). The whole thing is ~190 lines in `web/auth.py` and adds nothing
to `requirements-web.txt` — the same "don't add a layer that doesn't earn its
keep" rule the rest of the project follows.

**One middleware, two failure modes.** A single `@app.middleware("http")`
gates everything. It resolves the caller in priority order — session cookie first,
then `ORCH_WEB_TOKEN` bearer (so the CLI/API path keeps working as an implicit
admin) — and on failure does the *right kind* of rejection for the caller:
`401 JSON` for `/api/*`, a `302` to `/login` for page loads. Admin-only routes
(`/admin`, `/api/admin/*`) get a second check that returns `403`/redirect for
non-admins. SSE just works through it: `EventSource` can't send an `Authorization`
header, but it *does* send cookies, so the session authenticates the stream with
no special case. A short `_OPEN_PATHS` set (`/login`, `/api/login`, health,
favicon) and the `/static` prefix are the only holes.

**Per-user tool toggles, enforced server-side.** The right-hand panel lists every
discovered tool grouped by namespace, each with a switch and its `priv`/`confirm`
flags surfaced. The state stored is the **disabled set**, not the enabled set —
so a tool added later defaults to *on* without touching anyone's saved prefs. It
persists per user (`disabled_tools` JSON on the user row) and, crucially, is
applied in `/api/chat`: the server intersects the request's tool list with the
user's enabled set, so a disabled tool can't be smuggled back in by a hand-crafted
request body. This rides the `allowed`-tools plumbing already built for the
selector — the toggle is just another producer of that allowlist.

**Chats become per-user.** With logins there are now multiple owners, so
`chat` gained an `owner` column (added by an idempotent `PRAGMA table_info` +
`ALTER TABLE` migration — legacy rows keep `owner = NULL` and stay visible to
their creator). Every store method took an optional `owner` filter; the web layer
passes the session user, the bearer-token admin passes `None` (sees all). No
separate migration script — the store migrates itself on construction, the same
pattern the other SQLite stores use.

**The admin page replaces SSHing in.** `/admin` is one static page over a handful
of admin endpoints:

- **System-prompt editor** — `GET/PUT /api/admin/prompt` reads and writes the
  prompt file *and* updates the live `runtime.system_prompt`, so an edit takes
  effect on the next run with no restart.
- **Service status** — probes the LiteLLM proxy (and any `web.services` you list)
  with a short timeout, reporting up/down + latency, alongside process facts
  (uptime, active runs, tool count, brain model) and on-disk sizes of each store.
- **Run logs** — reads the existing trace DB (§14) directly: recent runs with
  status/duration, click-through to a run's event timeline. The trace was already
  there; this is just a reader.
- **User management** — create, reset password, grant/revoke admin, delete — with
  two guards that exist because they're the obvious foot-guns: you can't delete
  *yourself*, and you can't demote or delete the *last* admin (which would lock
  everyone out of `/admin` permanently).

**Testing.** All of it is covered the same way as the rest of the web layer —
against an in-process ASGI transport with the model mocked: unauthenticated
rejection (401 vs 302), login + cookie round-trip, toggle persistence, a *disabled
tool being filtered out of an actual run*, the live prompt edit, status/logs shape,
user CRUD including the last-admin guard, and per-user chat isolation (one user's
saved chat is invisible to another). The three pages are syntax-checked and the
tool-panel logic is exercised under a DOM stub. *Files:* `web/auth.py` (new),
`web/server.py`, `web/store.py`, `web/static/{index,login,admin}.html`,
`config/runtime.yaml` (new `web:` keys: `users_db`, `cookie_secure`, `services`).

> **First-boot and hardening.** On an empty user table the store seeds an admin
> from `ORCH_ADMIN_USER`/`ORCH_ADMIN_PASSWORD`, or generates a password and logs
> it once. Set `ORCH_SESSION_SECRET` to keep sessions across restarts, and flip
> `cookie_secure: true` the moment it's behind TLS so the cookie is never sent in
> the clear. The bearer token still works for scripts — it's just now one identity
> among several rather than the only gate.

### 17.2 Two-factor authentication (TOTP)

Once login exists, 2FA is a small, well-contained addition — because it lives at
a single choke point. The session middleware and signed cookie don't change at
all; 2FA is only a condition on *issuing* that cookie in `/api/login`, so nothing
on the per-request hot path moves. (Contrast a "2FA tool": access control runs
*before* any session or run exists, with no model in the loop, so it must sit
upstream of everything the agent can touch — never on the tool registry, where a
confused or injected brain could reach the very check guarding the account.)

**Dependency-free TOTP.** Codes are RFC 6238 TOTP verified with stdlib `hmac` —
about fifteen lines, no `pyotp`, the same "earn the dependency" rule that produced
the hand-rolled session cookie. `verify_totp` checks the current 30-second step
±1, so a slightly skewed phone clock still works.

**Enrollment is two-phase so a fumble can't lock you out.** `setup` generates a
*pending* secret and returns it plus an `otpauth://` URI (manual key entry — no QR
dependency); 2FA is not active yet. `confirm` verifies the first code against the
pending secret and only then promotes it to active, mints ten one-time backup
codes (returned once, stored pbkdf2-hashed like passwords), and clears the pending
slot. The three new columns (`totp_secret`, `totp_pending`, `backup_codes`) arrive
through the same idempotent `PRAGMA table_info` + `ALTER TABLE` migration as the
`owner` column.

**Login stays single-step.** The form sends username + password; if the account
has 2FA the server replies `401` with the detail `totp_required`, and the page
reveals a code field and resubmits. A valid TOTP *or* an unused backup code
passes; backup codes are consumed on use. Self-service disable requires a current
code; an admin `…/2fa/reset` clears a locked-out user's secret.

**Lockout recovery is the part to design, not the crypto.** Three escape hatches:
backup codes, the admin reset, and direct `users.db` access on the box. Note too
that the `ORCH_WEB_TOKEN` bearer bypasses `/api/login` entirely and so inherently
skips 2FA — handy as break-glass automation, but it makes the token itself the
thing to guard. *Files:* `web/auth.py` (TOTP helpers, columns, methods),
`web/server.py` (login check, `/api/2fa/*`, admin reset),
`web/static/{login,index,admin}.html`. Verified against the in-process app:
enrollment, login demanding the code (missing/wrong/valid), single-use backup
codes, admin reset with non-admins blocked, and self-disable.

## 18. The self-test harness (`test.run`)

The testing philosophy was real but external: each feature in this section was
checked against the app *in-process* with mocks before shipping — but those checks
were throwaway scripts, not a committed suite, and nothing let the *orchestrator
itself* run them. This phase closes the gap, and the design turns on the same
distinction as §17: enforcing auth is not a tool, but *testing code* is exactly
an "action during a run," so here a tool is the right shape.

**The idea: in-process, mocked, hermetic.** FastAPI speaks ASGI, so instead of
binding a port, `httpx` talks that contract directly in memory via
`ASGITransport` — a real request through real routing, middleware, validation and
cookies, with no socket and no server. Externals the box can't reach in a test
(the model behind LiteLLM, cloud providers) are swapped at their boundary: replace
`runtime.run` with a recorder coroutine, point a probe at an `httpx` stub.
Everything *we* wrote runs for real; only the outside world is faked. Fast,
deterministic, repeatable — exactly how the rest of this section was verified.

**Option C: one tool, two modes.** `test.run` (private, `requires_confirmation`,
like `job.*`) takes the agent's test as `test` (one file) or `files` (a map, for
multi-file suites and `conftest.py`), writes them to an isolated workdir, and runs
them against a venv that actually has the test deps, with the project root on
`PYTHONPATH` so tests can `import web.server`, `runtime.*`, `tools.*`. *Quick* mode
runs `pytest -q` as a bounded subprocess and returns parsed counts inline
(`{passed, failed, errors, skipped, ok, …}`); *detached* mode hands the identical
command to `job.start`, so a long suite runs in the background, tracked by the
existing `job.status`/`job.logs`/`job.list` — a zero exit code means it passed.
The fallback reuses machinery already built rather than adding a second job system.

**Two gotchas worth recording**, both from the subprocess running with `cwd` set
to the isolated workdir: (1) the configured `project_root` is `resolve()`d to an
absolute path before going on `PYTHONPATH`, or a relative value would point at the
wrong place; and (2) the documented test idiom locates project files via the
importable package (`Path(web.__file__).parent.parent`), never via the cwd —
otherwise `open("config/runtime.yaml")` fails inside the workdir. That idiom lives
in `docs/testing-harness.md` alongside the canonical ASGI/mock example and the
reusable patterns (mock at the boundary, throwaway DB paths, deterministic time
for TOTP, `401`-vs-`302` for a gated API vs a gated page). The example isn't just
illustrative — it runs green *through the tool*.

**What it can't do** is tell you a *live* external agrees — that the real LiteLLM
streams usage, or a phone authenticator's code is accepted. Those stay manual; the
harness covers everything below that line, which is most of it. *Files:*
`tools/test/{__init__,runner}.py`, `config/runtime.yaml` (`tools.test` plus `test`
in the privacy list), `requirements-test.txt`, `docs/testing-harness.md`.

## 19. Sub-agents (`agent.spawn`)

The single-agent toolkit was complete — retrieve, compute, persist, delegate to
cloud, self-test. Sub-agents are the first addition that changes what the system
*is* rather than what it can reach: the parent hands a subtask to a nested agent
and gets back only the distilled result, so the working detail never touches the
parent's context.

**The core win is context economy, not parallelism.** On a single-user box with
one local brain, three children that all hit the GPU-0 model just queue — there is
no throughput gain. What you get *every* time is isolation: a research child can
make a dozen `web`/`rag` calls in its own context and return a 200-word brief
while the parent's window stays small, cheap, and coherent, so a long
"research-then-act" session goes much further before it fills. Real parallelism
only arrives when children fan out to cloud models or a second local model
(GPU 1) — which is exactly where `agent.spawn` and the `model` override compose.

**A seam, not a special case.** The loop owns a `ctx.spawn(task, …)` closure,
threaded onto `ToolContext` the same way `on_event` and `confirm_provider` are;
the `agent.spawn` tool is a thin front door that calls it. The tool knows nothing
about runtime internals; the loop keeps all the wiring. A spawned child is just
another `run()` — its own `run_id`, trace row, and `Budget` — with `on_event=None`
so its steps don't flood the parent's live stream (that *is* the isolation, made
literal). `run()` gained two parameters for this: `model` (a per-run brain
override) and `depth`.

**Four properties the nesting has to get right:**

- **Budget composition.** The child's sub-budget is *clamped to the parent's
  remaining allowance* (cost, tokens, wall-clock), iterations defaulting to
  `agent.default_sub_iterations`. When the child finishes, its measured spend is
  reconciled back into the parent from the child's summary, so the parent's
  ceilings account for it and the next `tick()` enforces — the same "absorb, then
  enforce on next tick" rhythm the loop already uses for a tool's own LLM usage. A
  runaway child hits its own cap; it can't quietly drain the whole run.
- **Confirmation still reaches the human.** A child with `on_event=None` would
  otherwise swallow its own confirmation prompts, so a tiny `_NestedConfirm`
  wrapper routes the child's request to the *parent's* provider with the
  *parent's* `run_id` and emit — a child's `fs.write` prompts you on the stream
  you're already watching, and `/approve` lines up. Nesting never auto-grants.
- **Least privilege, now a hard boundary.** A child's tool set is *intersected*
  with the parent's — it can only narrow, never escalate. This forced a real fix:
  the selected allowlist used to gate only tool *exposure* (which schemas the
  model sees), not *execution*. It's now enforced at execution too, so a research
  child restricted to `web.search` cannot run `fs.write` even if it names it
  directly. (Bonus: this also hardens the web tool-toggles — a disabled tool is
  blocked, not merely hidden — and is a no-op for default `mode: all` runs, where
  the allowlist is `None`.)
- **Depth limit.** Spawning past `agent.max_depth` is refused with a clear error
  rather than recursing without bound.

**Privacy.** A sub-agent may have touched private tools, so `agent.*` is in
`private_tool_namespaces`: its result can't be forwarded to a remote `llm.call`
unless the run allows private sharing. Conservative by default; the parent's own
local reasoning can use it freely.

**Tested** against a mocked brain: context isolation (only the brief returns),
budget reconciliation (parent cost = parent + child), confirmation routed with the
parent `run_id`, the allowlist blocking a child's out-of-scope `fs.write` at
execution, the depth cap, and a regression that a plain no-spawn run is unchanged
(default model, no enforcement). *Files:* `runtime/loop.py` (`ctx.spawn`,
`_NestedConfirm`, `model`/`depth` params, execution-time allowlist enforcement),
`runtime/tool_base.py` (`spawn` seam on `ToolContext`),
`tools/agent/{__init__,spawn}.py`, `config/runtime.yaml` (`agent:` block + `agent`
private), `prompts/orchestrator.md`.

## 20. Runtime-loadable skills (`skill.load`)

By phase 12 the toolkit was complete, but every capability had to be either a
built-in tool or baked into the system prompt. Skills add a third layer:
**packaged playbooks the model loads on demand** — so niche know-how (how to read
a `.docx`, how to serve a model on GPU 1) lives on disk, not in every context, and
the model pulls in only what the task in front of it needs.

**Why progressive disclosure is the whole design.** A naive version — "a tool that
returns a prompt" — fails the moment it matters: a `.docx` arrives and the model
never thinks to ask for help, because it doesn't know a docx skill exists. So
skills are two-tier, exactly like Anthropic's own Skills. A lightweight **catalog**
(each skill's name + a one-line "when to use it") is injected into the system
prompt at startup — cheap, always present, enough for the model to recognise a
match. The heavy **body** (the actual instructions, often long) is returned only
when the model calls `skill.load(<name>)`. The `description` frontmatter *is* the
trigger; it's written to say plainly when to load ("Load when a .pdf is uploaded or
referenced"), which is the steering the prompt would otherwise have to carry.

**A skill is instructions, not a new tool.** Loading a skill doesn't register code
at runtime — it drops a playbook into the conversation that the model then executes
*with the tools it already has*. This is why skills compose: the docx skill tells
the model to run a bundled stdlib script via `job.start`; long-document
orchestrates `rag.index`/`agent.spawn`; gpu-serve drives `gpu.status` +
`job.start(gpus="1")`. One real constraint shaped the file skills: `code.execute`
is stdlib-only with a tight `allowed_imports` (no `zipfile`/`xml`/`csv`), so
anything needing those runs as a `job.start` instead — each skill says so, and
notes the inline `code.execute` path as an option if those imports are ever
whitelisted.

**The mechanism** (`runtime/skills.py`, the single source of truth for both tiers):
`discover_skills(dir)` scans `skills/<name>/SKILL.md`, parsing YAML frontmatter
(`name`, `description`) and body, plus a manifest of bundled files.
`render_catalog()` produces the names-and-descriptions block the loop appends to
the system message (bodies never leak into it). `load_skill()` returns a body plus
the **absolute paths** of bundled files, so the model can run a script or read a
reference. Two tools (`tools/skill/`): `skill.load(name)` and `skill.list` — both
non-private (instructions aren't user data) and non-confirmation (loading text is
harmless; any action a skill then suggests goes through that action's own gate).
The loop discovers skills once in `__init__` and injects the catalog above the base
prompt; this is also where `read_text` gained `errors="replace"`, so a stray
non-UTF-8 byte in a prompt or skill can no longer crash the service into a restart
loop.

**The starter pack** (10 skills) splits into file-readers and workflow playbooks:

- *OOXML by stdlib* — **docx**, **xlsx**, **pptx**. Each is a ZIP of XML, so a
  bundled standard-library script (`zipfile` + `ElementTree`) extracts the text
  with zero dependencies, run via `job.start`. They're honest that they get *text*,
  not layout/formulas/styles, and point to `python-docx`/`openpyxl`/`python-pptx`
  in a venv for fidelity.
- *Needs a library or binary* — **pdf** (`pypdf` in a venv, with an
  `ocrmypdf`/`tesseract` branch for scanned pages) and **image** (`tesseract` OCR).
  These name their dependency and instruct the model to *say so rather than guess*
  if it's missing — the anti-hallucination rule that makes file skills trustworthy.
  The image skill is also straight about a real gap: `llm.call` is text-only, so
  *visual* understanding (beyond OCR) isn't possible until a multimodal `content`
  array is added to it.
- *Stdlib utility* — **archives** (zip/tar/tgz/bz2 list + extract, with a zip-slip
  guard).
- *Workflow playbooks (no scripts)* — **long-document** (map-reduce via
  `agent.spawn`, or index into `rag`), **codebase-review** (orient → read
  selectively → change → verify with `test.run`), **web-research** (facet → gather
  → cross-check ≥2 sources → synthesise with URLs), and **gpu-serve**, which encodes
  this box's specifics: check VRAM with `gpu.status`, launch on GPU 1 via
  `job.start(gpus="1", source_env=true)` using the existing presets, the
  `HIP_VISIBLE_DEVICES`-not-`ROCR` gotcha, and the 32 GB budget.

This closes a loop with chat uploads: drop a `.docx` in the chat → the attachment
note records its path → the catalog tells the model the docx skill exists → it
loads the skill → runs the bundled extractor on that path.

**Adding skills is a pure content operation** — `mkdir skills/<name>/`, write a
`SKILL.md` with a sharp `description`, drop in any scripts, restart. No code or
prompt change, because the prompt is deliberately decoupled from the skill set: it
teaches the *mechanism* (`skill.load`/`skill.list`) once and lets the
runtime-rendered catalog carry the rest. The one future trigger for revisiting: if
the catalog ever grows long enough to be worth trimming, the move is to switch
discovery to a `skill.list`-on-demand model rather than hardcoding skills into the
prompt.

**Tested** against the registry and with hand-built fixtures: discovery + catalog
(no body leakage), `skill.load` returning body + file paths (and a helpful error
listing what's available on a miss), catalog injection into the system message, the
stdlib `xlsx`/`pptx`/`archives` extractors against fabricated files, and
`read_pdf.py` against a generated two-page PDF. *Files:* `runtime/skills.py`,
`tools/skill/{__init__,load}.py`, `runtime/loop.py` (catalog injection + UTF-8-safe
prompt read), `config/runtime.yaml` (`skills.dir`), `prompts/orchestrator.md`
(catalog entry + sequencing note), and `skills/<name>/` for each.

## 21. Serving models on the GPUs (`serve.*`)

For a long time the second R9700 sat idle while every model call queued behind the
single brain on GPU 0. The gpu-serve *skill* (§20) documented how to bring a second
server up by hand with `job.start`, but "documented in a playbook" isn't "managed":
nothing tracked what was running, freed it, or made it callable. `serve.*` promotes
that recipe to a first-class capability.

**What it manages.** `serve.start` launches a model server — a second LLM, an
embedder, or a reranker — pinned to GPU 1 by default so GPU 0 stays the brain's. It
reuses the job runner's detached-launch mechanics verbatim (its own session so the
server survives an orchestrator restart, `GPU_MAX_HW_QUEUES=1`,
`HIP_VISIBLE_DEVICES` alone, optionally sourcing `rdna4-env.sh`), then adds what
makes it a *managed* server rather than a fire-and-forget job: it picks a free port
(avoiding the brain's 8090 and LiteLLM's 4000), does an advisory VRAM headroom check
through the same rocm-smi parser `gpu.status` uses, waits for the server to answer
`/health`, and reads the served model id back from `/v1/models`. `serve.list`,
`serve.status` (with a live health probe), and `serve.health` inspect; `serve.stop`
SIGTERMs the process group, frees the VRAM, and tidies up. The registry is the
filesystem — one directory per server under `state_dir/` — so liveness is derived
from the live pid, exactly like the job runner and delivered outputs.

**Why this is the keystone.** Three things already built were waiting on it.
`agent.spawn(model=...)` (§19) could override a sub-agent's model, but there was
only one local model to override *to*; a small fast model on GPU 1 means a spawned
sub-agent can run genuinely in parallel with the brain instead of serializing behind
it. RAG's embedder and reranker (§13) were whatever happened to be listening on the
configured URLs; `serve.start(kind="embedding", wire_rag=true)` brings one up and
points `rag.*` at it for the session. And "free VRAM before loading something
bigger" became one call instead of hunting for a pid.

**Callability — the LiteLLM seam.** Everything here calls models through LiteLLM at
`litellm_base` (the brain loop and `llm.call` both do). So for a served model to be
reachable *by name* — `agent.spawn(model="fast-llm")` — it has to be a LiteLLM
alias, not just an open port. `serve.start` registers one best-effort via the
proxy's runtime model API and records the id so `serve.stop` can deregister it. The
honest part: that API depends on the proxy being configured to allow runtime adds,
so registration can fail — in which case the tool degrades gracefully, returning the
direct `…/v1` URL and telling you to add a static alias. The lifecycle never depends
on registration succeeding.

**Seams it needed.** Almost none — which is the dividend of building the foundations
first. The launch logic is the job runner's; the VRAM read is `gpu.status`'s parser;
the confirmation gate (§14) already covers it because `serve.start` is
`requires_confirmation` (it consumes a scarce resource); and the registry is the
same filesystem-as-truth pattern used elsewhere. The only genuinely new piece is the
LiteLLM registration call.

**Tested** against a fake model server and a fake LiteLLM admin: discovery and the
confirmation flag, free-port selection skipping reserved/taken ports, the rocm-smi
VRAM parse, a full start → register → list → status → health → stop cycle (the
process actually killed, the registry cleared, the alias deregistered), refusal of a
duplicate name, and the `wire_rag` runtime-config mutation. *Files:*
`runtime/serving.py` (registry, launch, health, VRAM, LiteLLM registration),
`tools/serve/{__init__,lifecycle}.py` (`serve.start/stop/status/list/health`),
`config/runtime.yaml` (`tools.serve`). The gpu-serve skill (§20) now teaches
`serve.start` as the primary path, with raw `job.start` only as a fallback.

## 22. Bugs the tests caught

Three real bugs surfaced only by *running* the integration tests, each invisible
to careful reading. They're worth recording because they're representative.

- **Zombie PID in `job.status`.** `os.kill(pid, 0)` succeeds for a process that
  has exited but not been reaped (a zombie), so a cancelled job kept reporting
  `running`. Fix: a `/proc/<pid>/stat` state check that treats `Z` as dead, plus
  `job.cancel` writing a deterministic exit marker so status is unambiguous.
- **Unbound `args` in the `tool_result` event.** When a tool call's JSON arguments
  failed to parse, the new event field referenced an `args` variable that was only
  bound on the happy path → `NameError`. Fix: bind `args = None` in the `except`.
  Lesson: a newly added event field can reach a variable that isn't set on every
  branch.
- **FastAPI 422 on `/api/approve`.** The body model was defined *inside*
  `create_app`; with `from __future__ import annotations` active, FastAPI resolves
  hints via `get_type_hints` against *module* globals, couldn't find the nested
  class, and fell back to treating the body as a query parameter. Fix: module-level
  Pydantic models. Lesson: deferred annotations + locally-defined types is a trap
  in any framework that introspects signatures.

The takeaway is the project's testing philosophy in miniature: each bug was a
20-line test away from obvious, which is why every piece — loop, tools, gate,
events, endpoints — got an integration test before shipping.

## 23. Still ahead

- **Larger-corpus RAG** — the Qdrant/HNSW swap behind the unchanged `rag.*`
  interface (§13). Deferred until a single collection crosses ~tens of
  thousands of chunks, query latency becomes noticeable, or metadata-filtered /
  hybrid search is wanted — whichever comes first.
- **Image input for `llm.call`** — a multimodal `content` array so the image skill
  can do visual understanding, not just OCR.

(Multi-turn and token/cost streaming, previously listed here, shipped in §15.)

Each rests on the built foundations rather than replacing them — the same
discipline that kept this whole part incremental.

---

## 24. Glossary

| Term | Definition |
|------|-----------|
| **Agent** | A program that uses an LLM in a loop, where the LLM's output influences subsequent inputs (typically by selecting tools to call). |
| **Agent loop** | The cycle of `call model → receive tool calls → execute → feed results back`, repeated until the model produces a final answer or a limit is hit. |
| **Tool call** | Structured output from a model in the form `{"name": "...", "arguments": {...}}` indicating it wants a function to be invoked. |
| **Tool result** | A message with `role: "tool"` appended to the conversation, containing what the executed tool returned. |
| **System prompt** | The first message in a conversation, with `role: "system"`. Sets behavior, persona, constraints. |
| **Context window** | The maximum number of tokens a model can attend to in a single call. Qwen3-30B-A3B has 128k; smaller models often 8k–32k. |
| **Token** | The unit a tokenizer splits text into. Roughly 0.75 words of English. Cost is per-token, both for input and output, usually at different rates. |
| **Prompt caching** | Provider-side optimization where unchanged prefix tokens are kept in a fast cache and billed at a fraction of normal cost. |
| **MoE** | Mixture-of-Experts — a model architecture where only a subset of parameters is used per token. Qwen3-30B-A3B has 30B total params, ~3B active. |
| **Quantization** | Lossy compression of model weights from FP16 to smaller types (Q8, Q4, IQ4_XS, etc.). Trades quality for VRAM and speed. |
| **K-quants** | Modern llama.cpp quantization family (Q2_K through Q6_K) with two-level block structure and mixed precision across layers. |
| **I-quants** | Importance-matrix-calibrated quantization (IQ2_XS through IQ4_NL). Slightly smaller and higher quality than K-quants at the same bit count, slightly slower to compute. |
| **imatrix** | Importance matrix — a calibration file generated by running representative text through the FP16 model. Used by I-quants (and optionally by K-quants) to allocate bits to weights that matter most. |
| **bpw** | Bits per weight — the average storage cost across all weights in a quantized model. A Q4_K_M model averages ~4.5 bpw. |
| **Perplexity (PPL)** | Standard metric for measuring quantization quality. Lower is better; measures how "surprised" the model is by held-out text relative to its FP16 baseline. |
| **GGUF** | The file format llama.cpp uses for quantized models. |
| **KV cache** | Per-conversation cached attention state. Lives in VRAM. Sized roughly proportional to context length × hidden size × layers. |
| **Tool schema** | JSON Schema describing a tool's name, description, and arguments. Sent to the model so it knows what's available. |
| **RAG** | Retrieval-Augmented Generation — pattern where you retrieve relevant documents from a vector DB and inject them into the prompt before generation. |
| **Embedder** | A model that maps text to a fixed-dimensional vector (typically 768–4096 dims). Bi-encoder: encodes query and documents independently for fast similarity search. |
| **Reranker** | A cross-encoder model that scores a (query, document) pair jointly. More accurate than an embedder but slower; used as a second-stage filter on top-N candidates. |
| **Bi-encoder vs cross-encoder** | Two ways to compare texts. Bi-encoder = embed each separately, compare vectors (fast, less precise). Cross-encoder = jointly process pair, output score (slow, more precise). |
| **MTEB** | Massive Text Embedding Benchmark — the standard leaderboard for embedding models, with English, multilingual, code, and other subsets. |
| **Recall@K / MRR** | RAG quality metrics. Recall@K: did the right chunk land in the top-K? MRR (mean reciprocal rank): how high on average? |
| **Matryoshka embeddings** | Training technique that lets you truncate output vectors to smaller dimensions without retraining, with graceful quality degradation. |
| **TEI** | text-embeddings-inference — HuggingFace's purpose-built server for embedding and reranking models. |
| **Vector DB** | Database optimized for nearest-neighbor search over high-dimensional vectors. Qdrant, Milvus, Weaviate, pgvector are common choices. |
| **MCP** | Model Context Protocol — Anthropic-originated open protocol for connecting LLMs to external tools and data sources via a standardized server interface. |
| **Sandbox** | An execution environment with restricted access to the host (network, filesystem, syscalls). We use firejail for `code.execute`. |
| **LiteLLM** | Open-source proxy that exposes a unified OpenAI-compatible API across many LLM providers. |
| **Trace** | A persistent log of every step an agent took during a run, used for debugging and replay. |
| **Detached job** | A long-running command spawned in its own session so it outlives the agent loop's budget and survives an orchestrator restart; polled via `job.status`/`job.logs` rather than awaited (§12). |
| **Knowledge graph (kg)** | Typed entities plus directed relations between them, stored so they can be queried and traversed; the structured half of the local "world model" (§12). |
| **World model (local)** | Here, the self-maintained state the orchestrator carries across runs — `memory.*` (free-form facts) and `kg.*` (structured graph) — as opposed to read-only document retrieval (§12). |
| **FTS5** | SQLite's full-text search extension; backs `memory.search` (§12). |
| **Cosine similarity** | The nearest-neighbour metric used by `rag.search` over normalized embedding vectors (§13). |
| **Confirmation gate** | The mechanism by which `requires_confirmation` tools pause for human approval; *whether* to ask lives in the loop, *how* in a pluggable provider (§14). |
| **Event sink / EventBus** | The transport-neutral seam that streams every run step to a subscriber (UI, log) without the loop knowing about HTTP; backs the live console (§14). |
| **SSE** | Server-Sent Events — one-way server→client HTTP streaming with auto-reconnect and `Last-Event-ID` resume; the web console's "watch the loop" channel (§15). |
| **Confirmation provider** | The object that decides how a gated tool asks for approval — CLI TTY prompt vs the web `POST /api/approve` Future (§14, §15). |

---

## 25. Further reading

- **Anthropic's "Building effective agents"** — short blog post, foundational reading on agent design patterns.
- **The llama.cpp README and CLI docs** — the only documentation for the local inference stack. The `tools/server/README.md` in particular.
- **llama-swap on GitHub** (mostlygeek/llama-swap) — when you outgrow Option A from section 8.5.
- **"Which Quantization Should I Use?"** (arXiv:2601.14277) — empirical study of llama.cpp quantization on Llama 3.1, ties section 9's theory to measured numbers.
- **The llama.cpp quantization discussion** (github.com/ggml-org/llama.cpp/discussions/2094) — the canonical thread where the K-quant and I-quant designs were debated. Long but illuminating.
- **Qwen3-Embedding paper** (arXiv:2506.05176) — the embedder and reranker family used in section 10.
- **MTEB leaderboard** (huggingface.co/spaces/mteb/leaderboard) — see what's currently winning at embedding benchmarks.
- **text-embeddings-inference docs** (huggingface.co/docs/text-embeddings-inference) — TEI is the recommended serving layer for the RAG stack.
- **MCP spec at modelcontextprotocol.io** and the **MCP Python SDK** (github.com/modelcontextprotocol/python-sdk) — the source of truth for the bridge, if/when §12's deferral ends.
- **The Qwen3 model card on HuggingFace** — Jinja template, recommended sampling params, tool-calling notes.
- **LiteLLM docs** — especially the proxy section. Their fallback/retry/routing config is more sophisticated than what we use here.
- **The Qdrant tutorials** — for the §13 backend swap when brute-force cosine stops scaling. Their hybrid search guide is the most useful one.
- **FastAPI docs + `sse-starlette`** (github.com/sysid/sse-starlette) — the web console's stack (§15); the SSE section and `EventSourceResponse` in particular.
- **MDN: Server-Sent Events / `EventSource`** — the browser side of §15, including auto-reconnect and `Last-Event-ID` semantics.

For deeper theory:
- Karpathy's "Let's build GPT" video series — if you want to understand what's
  inside the box you're orchestrating.
- The original "Toolformer" paper (Schick et al., 2023) — first formalized
  tool-augmented LM behavior.
- The ReAct paper (Yao et al., 2022) — the reasoning loop pattern you've
  implemented has this lineage.

---

## Appendix: where each file is built

Every file in the `phase 1–4` tarball has a **Build it yourself** block in the
section that explains its concept. Use this as a checklist when reconstructing
the project from scratch.

| File | Built in |
|------|----------|
| directory skeleton, `__init__.py` stubs, `README.md` | §1 |
| `prompts/orchestrator.md` | §2 (after 2.6) |
| `config/litellm.yaml` | §3.1 |
| `config/runtime.yaml` | §3 (end) |
| `requirements.txt`, `requirements-litellm.txt`, `~/.config/orchestrator.env`, `systemd/llama-orchestrator.service`, `systemd/litellm-proxy.service` | §5 (end) |
| `scripts/orch` | §6 (end) |
| `runtime/tool_base.py` | §7, Stop 1 |
| `runtime/registry.py` | §7, Stop 2 |
| `runtime/budget.py` | §7, Stop 3 |
| `runtime/trace.py` | §7, Stop 4 |
| `runtime/loop.py` | §7, Stop 5 |
| `runtime/selector.py` *(optional)* | §3.7 |
| `tools/llm/cloud_models.py` | §7, Stop 6 |
| `tools/web/search_fetch.py` | §7, Stop 7 |
| `tools/code/execute.py` | §7, Stop 8 |
| `scripts/start-llama.sh`, `config/qwen3-tools.jinja` | §8 (after 8.2) |

Phases 5–14 add the files below. They're documented as design + decisions across §§12–23
(not full *Build it yourself* listings); the repo files are the source of truth.

| File | Documented in |
|------|----------|
| `tools/job/runner.py` | §12 |
| `tools/fs/ops.py` | §12 |
| `tools/git/status.py` | §12 |
| `tools/gpu/status.py` | §12 |
| `tools/memory/store.py`, `tools/kg/graph.py` | §12 |
| `tools/arxiv/search.py`, `tools/eval/compare.py` | §12 |
| `tools/rag/store.py` | §13 |
| `runtime/confirm.py` + `runtime/loop.py` `_confirm` | §14 |
| `runtime/events.py` + `runtime/loop.py` `emit`/`on_event` | §14 |
| `web/server.py`, `web/static/index.html`, `systemd/orchestrator-web.service`, `requirements-web.txt` | §15 |
| `web/store.py` | §15 |
| multi-turn + streaming: `runtime/loop.py` (`history`/`stream`, `_model_turn_streaming`), `runtime/tool_base.py` (`on_token`), `tools/llm/cloud_models.py`, `web/server.py`, `web/static/index.html` | §15 |
| auth + admin: `web/auth.py` (new), `web/server.py` (middleware + tool/admin endpoints), `web/store.py` (`owner` column), `web/static/{login,admin}.html` (new) + `index.html` (tools panel, user menu), `config/runtime.yaml` (`web:` keys) | §17 |
| 2FA (TOTP): `web/auth.py` (TOTP helpers + `totp_secret`/`totp_pending`/`backup_codes`), `web/server.py` (`/api/2fa/*`, admin reset, login check), `web/static/{login,index,admin}.html` | §17 |
| self-test harness: `tools/test/{__init__,runner}.py` (`test.run`), `config/runtime.yaml` (`tools.test` + privacy), `requirements-test.txt`, `docs/testing-harness.md` | §18 |
| sub-agents: `runtime/loop.py` (`ctx.spawn`, `_NestedConfirm`, `model`/`depth`, exec-time allowlist), `runtime/tool_base.py` (`spawn` seam), `tools/agent/{__init__,spawn}.py`, `config/runtime.yaml` (`agent:` block + privacy) | §19 |
| skills: `runtime/skills.py` (discover/catalog/load), `tools/skill/{__init__,load}.py` (`skill.load`/`skill.list`), `runtime/loop.py` (catalog injection), `config/runtime.yaml` (`skills.dir`), `skills/<name>/SKILL.md` (+ bundled scripts) | §20 |
| model serving: `runtime/serving.py` (registry/launch/health/VRAM/LiteLLM), `tools/serve/{__init__,lifecycle}.py` (`serve.start/stop/status/list/health`), `config/runtime.yaml` (`tools.serve`) | §21 |

Suggested build order: §1 skeleton → §7 Stops 1–5 (runtime core, in order) →
§2 prompt + §3 configs → §7 Stops 6–8 (the three starter tools) → §8 launcher +
template → §5 services → §6 CLI → smoke test (§5.8). Then, optionally, layer on
§3.7's `runtime/selector.py` for cache-safe per-run tool selection.

---

*Last updated alongside phase 14: model serving (`serve.*`) — managed start/stop/health/list for model servers on the GPUs, pinned to GPU 1, with best-effort LiteLLM registration so a served model is callable by name and a spawned sub-agent can run in parallel with the brain (§21) — on top of phase 13's runtime-loadable skills, phase 12's sub-agents, phase 11's self-test harness and TOTP two-factor, phase 10's access control, and phases 5–9 (the capability tools, RAG on SQLite+numpy, the confirmation gate, the event sink, and the FastAPI + SSE web console with saved-chat history) (§§12–23). Phases 1–4 keep their full Build-it-yourself blocks; 5–14 are documented as design + decisions, with the repo files as source of truth. When you finish the next phase — larger-corpus RAG, or image input for `llm.call` — come back and add a section.*
