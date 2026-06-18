# JayNet Orchestrator — Technology Map

A one-page tour of the stack: what each piece is, why it's there, and how they
connect. Distilled from the Learning Guide. The system is a **local, bounded LLM
agent** — a model that reasons in a loop and calls tools, running on your own
hardware with cloud models available only when you opt in.

The one idea that ties everything together: **every model is an HTTP service
speaking the OpenAI chat-completions API.** Local or cloud, brain or embedder,
they all look the same to the code, so components are swappable and the seams are
all just HTTP.

---

## The layers (bottom to top)

### 1. Hardware & OS
- **Arch Linux / CachyOS, Ryzen 9 7950X, 64 GB RAM, 2× AMD Radeon AI PRO R9700** (RDNA4, 32 GB each), **ROCm 7.x**. — The metal. GPUs run the models; the CPU runs the orchestrator and the RAG models. *Links up to:* the inference layer (GPU compute) and the Python runtime (CPU).

### 2. Inference engine — turning weights into tokens
- **llama.cpp / `llama-server`** — runs GGUF models and exposes an OpenAI-compatible HTTP endpoint. Built against **ROCm/HIP** for the AMD cards (a Vulkan build exists as a fallback). *This is the workhorse:* the brain, the embedder, and the reranker are each just a `llama-server` process on a different port.
- **GGUF + quantization** (Q8_0, Q4_K_M, i-quants) — the on-disk model format and the size/quality trade-off knob. Smaller quants fit more in VRAM/RAM at some accuracy cost. *Use-case:* fit a 35B brain on one GPU; keep the embedder near-lossless at Q8.
- **Models**: **Qwen3-35B-A3B MoE** (the orchestrator "brain", GPU0), **Qwen3-Embedding-8B** + **Qwen3-Reranker-0.6B** (RAG, on CPU), plus **stable-diffusion.cpp / FLUX** for images. *Links up to:* LiteLLM (brain) and the RAG subsystem (embedder/reranker).

### 3. Model gateway
- **LiteLLM proxy** (`:4000`) — one OpenAI-compatible API in front of *everything*: the local brain (`llama-server :8090`) and cloud models (Claude, Gemini, Qwen). It also does cost tracking and fallbacks. *Use-case:* the orchestrator targets one endpoint; switching or adding a model is a config line, not a code change. *Links:* sits between the agent loop and all LLMs.

### 4. Orchestrator core — the agent (Python 3.13)
- **The bounded agent loop** (`runtime/loop.py`) — the heart. It sends the conversation to the brain, reads back tool calls, runs them, feeds results in, and repeats until the model answers or a budget runs out. *Connects:* LiteLLM (to think) ↔ the tools (to act).
- **Plugin tool registry** (`runtime/registry.py`, `tool_base.py`) — auto-discovers every `Tool` subclass under `tools/`. *Use-case:* adding a capability is one new file; no central wiring.
- **Budget** (`runtime/budget.py`) — hard ceilings on iterations, wall-clock, cost, tokens. *Use-case:* a runaway loop stops itself.
- **Confirmation gate** (`runtime/confirm.py`) — pauses state-changing or cloud-reaching tool calls for human approval. *Links to:* the web console (which renders the approve/deny prompt).
- **Trace** (`runtime/trace.py`, SQLite) — records every step for debugging and cost accounting.
- **Skills** (`runtime/skills.py`, `skills/*/SKILL.md`) — short playbooks injected as a catalog; the model loads one on demand. *Use-case:* reusable know-how (web research, self-test) without bloating every prompt.
- **Sub-agents** (`agent.spawn`) — the loop can spawn nested loops with their own budget. *Use-case:* fan-out research while keeping raw pages out of the main context.

### 5. Tools — what the agent can actually do
Each is a plugin the loop can call. Grouped by purpose:
- **Reason/compute**: `code` (Python in a **firejail** sandbox), `llm` (call a *cloud* model via LiteLLM — gated by approval + privacy).
- **Read the world**: `web` (Tavily search + fetch), `arxiv`.
- **Knowledge & memory**: `rag` (search indexed docs), `memory` (durable facts), `kg` (a small knowledge graph).
- **Files & code**: `fs` (read/write under one allowed root), `git`, `deliver` (hand a file to the user as a download).
- **Infra**: `gpu` (status), `serve` (launch/stop extra models on GPU1 on the fly), `job` (background processes), `test`/`eval`.
- **Bridge**: `mcp` — connect external **Model Context Protocol** tool servers (the standard way to plug third-party capabilities in).

### 6. RAG subsystem — retrieval
- **Embedder + reranker** (`llama-server`, CPU) — turn text into vectors and re-score candidates. *Why CPU:* embedding/reranking is a single forward pass, cheap on the 7950X, and keeps both GPUs free.
- **SQLite vector store** (`rag.db`) + brute-force cosine, with the reranker as an optional second pass. *Use-case:* search your own indexed documents. *Links:* `rag.*` tools → embedder (`:8095`) / reranker (`:8096`).

### 7. Web layer — the console
- **FastAPI + uvicorn** (`web/server.py`) — the HTTP server: auth, `/api/chat`, file uploads, project files.
- **Server-Sent Events** (`sse-starlette`) — streams the run live (tokens, tool steps, cost) to the browser. *Use-case:* you watch the agent think in real time.
- **The console** (`web/static/`: `index.html` + `app.css` + `app.js`) — chat UI, tool toggles, run options, projects, and a **CodeMirror** code editor for project files.
- **Auth** — `users.db`, password + **TOTP 2-factor**, signed sessions (`session.secret`). *Links:* gates every `/api/*` route.

### 8. Persistence
- **SQLite** everywhere lightweight: `chats.db` (saved chats), `users.db` (accounts/2FA), `trace.db` (run logs), `rag.db` (vectors). Plus the **filesystem** for uploads, project files, and delivered outputs. *Use-case:* no separate database server to run.

### 9. Deployment & ops
- **systemd user services** — `llama-orchestrator` (brain), `litellm-proxy`, `orchestrator-web`, and the two `rag-*` services. *Use-case:* start on boot, restart on failure, one command to manage.
- **nginx reverse proxy** (separate host) + **Let's Encrypt TLS** — terminates HTTPS at `orch.jaynet.ch` and forwards to uvicorn. (Must stream, not buffer, for SSE and large static files.)
- **uv** — creates the Python 3.13 venvs with pinned versions. **cmake/HIP** — builds llama.cpp for the AMD cards.

---

## How a request flows (the linkage)

```
Browser (console, SSE) ──HTTPS──▶ nginx ──▶ uvicorn / FastAPI  (auth, /api/chat)
                                                   │
                                                   ▼
                                        AgentRuntime.run()  ── the loop ──┐
                                                   │                      │
                          builds messages + tool schemas                  │ each iteration:
                                                   ▼                      │
                                   LiteLLM proxy (:4000) ◀────────────────┘
                                       │            │
                          local brain  │            │  cloud (Claude/Gemini)  ← only via llm.call,
                      llama-server :8090            │     approval + privacy gated
                                       │
                          model emits tool calls
                                       ▼
            loop dispatches to tools ── budget · confirmation · privacy · trace
              │        │        │         │         │          │
             fs      web      rag       code      serve      agent.spawn
                                │                    │            │
                       embedder/reranker      extra model     nested loop
                        (:8095 / :8096)        on GPU1        (own budget)
                                       │
                          tool results fed back into the loop
                                       ▼
                     final answer ──▶ streamed back over SSE ──▶ Browser
```

**Reading it as a sentence:** the browser opens a streamed connection through nginx
to FastAPI, which authenticates you and starts the agent loop. The loop asks the
**brain** (through LiteLLM) what to do; the brain replies with tool calls; the loop
runs those tools — checking budget, asking for approval where needed, blocking
private data from leaving — and feeds the results back. It repeats until the brain
produces an answer, which streams to your screen token by token. RAG calls reach
the CPU embedder/reranker; `serve` spins up extra models on the spare GPU; cloud
calls go out only through `llm.call`, and only with your approval.

---

## The design throughline
- **One API everywhere** — the OpenAI chat-completions shape is the universal seam, so any model (local or cloud, brain or embedder) is interchangeable HTTP.
- **Local-first, cloud-on-request** — the brain runs on your GPUs; cloud is opt-in and gated.
- **Tools are plugins** — capabilities are added by dropping a file in `tools/`.
- **Bounded by default** — every run has hard budgets and a confirmation gate; private data is fenced from cloud calls by namespace.
- **Plain, inspectable parts** — SQLite, systemd, a readable ~200-line loop. Nothing you can't open and read.
