# Local LLM Orchestrator

A privacy-first multi-LLM agent runtime for a dual-R9700 Arch Linux workstation.
This is a learning project — the goal is to understand how agent loops, tool
plugins, budgets, RAG, and MCP fit together by building each piece yourself.

## Architecture

- **llama.cpp server** (`:8080`) — local orchestrator brain on dual R9700 (Vulkan).
- **LiteLLM proxy** (`:4000`) — unified OpenAI-compatible API for local + cloud LLMs.
- **Orchestrator runtime** — bounded agent loop with plugin tools, budgets, tracing.

This phase (1–4) gives you a working Level-1 agent:
- Reasoning loop with tool calls
- Three starter tool categories: cloud LLMs, web search/fetch, sandboxed code
- Budget enforcement (iterations, wall-clock, cost, tokens)
- SQLite trace logging
- Privacy gating between tool namespaces

Phases 5–8 land next: RAG, MCP bridge, filesystem tools, sub-agent loop.

## Layout

```
/srv/orchestrator/
├── config/             # litellm.yaml, runtime.yaml, qwen3-tools.jinja
├── runtime/            # agent loop, budget, trace, registry, tool base
├── tools/              # plugin tools, auto-discovered
│   ├── llm/            # cloud_models.py -> the llm.call tool
│   ├── web/            # search, fetch
│   ├── code/           # execute (sandboxed)
│   ├── rag/            # (phase 5)
│   ├── fs/             # (phase 6)
│   └── mcp/            # (phase 6)
├── agents/             # sub-agent YAML defs (phase 7)
├── prompts/            # system prompts
├── data/               # trace.db, litellm.db, qdrant/ (later)
├── systemd/            # user units
└── scripts/            # CLI + launchers
```

## Setup (Arch)

```bash
# System deps
sudo pacman -S python python-pip base-devel cmake git ninja firejail sqlite

# llama.cpp — REUSE your existing ROCm build (built/refreshed by build_tools.sh).
# The orchestrator does not compile its own; start-llama.sh defaults to:
#   /srv/llama/llama.cpp-rocm/build/bin/llama-server
# Build it once if you don't have it; rebuild after a ROCm-touching pacman -Syu:
/srv/llama/build_tools.sh llama rocm
/srv/llama/llama.cpp-rocm/build/bin/llama-server --version   # sanity check

# Orchestrator
sudo mkdir -p /srv/orchestrator && sudo chown $USER /srv/orchestrator
cp -r ./* /srv/orchestrator/
cd /srv/orchestrator

# Two SEPARATE venvs (both Python 3.13 — 3.14 currently breaks the LiteLLM proxy):
#   /srv/orchestrator/.venv      -> the runtime (agent loop + tools)
#   /srv/orchestrator/litellmenv -> the LiteLLM proxy ONLY (its pinned deps must
#                                   stay isolated; kept inside the project so the
#   orchestrator a self-contained, deletable unit)
# NOTE: uv-created venvs have NO `pip` inside — install with `uv pip --python`
# (or pass --seed to `uv venv` if you want a real pip in the venv).
uv venv /srv/orchestrator/.venv --python 3.13
uv pip install --python /srv/orchestrator/.venv/bin/python -r requirements.txt

uv venv /srv/orchestrator/litellmenv --python 3.13
uv pip install --python /srv/orchestrator/litellmenv/bin/python -r requirements-litellm.txt

# Env vars (only the providers in use)
cat > ~/.config/orchestrator.env <<EOF
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
DASHSCOPE_API_KEY=...                 # Qwen (Alibaba Model Studio)
TAVILY_API_KEY=tvly-...                # optional: web.search/fetch backend (else SearxNG/DDG)
LITELLM_MASTER_KEY=sk-local-orch-$(openssl rand -hex 16)
EOF
chmod 600 ~/.config/orchestrator.env

# Systemd user units
mkdir -p ~/.config/systemd/user
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now llama-orchestrator.service
systemctl --user enable --now litellm-proxy.service

# Test
./scripts/orch "What is 2+2? Use the code tool to verify."
```

## Shell aliases

```bash
alias orchenv='source /srv/orchestrator/.venv/bin/activate && set -a && source ~/.config/orchestrator.env && set +a'
alias litellmenv='source /srv/orchestrator/litellmenv/bin/activate'   # project-local proxy env
alias orch='/srv/orchestrator/scripts/orch'
alias orchlogs='journalctl --user -u llama-orchestrator.service -u litellm-proxy.service -f'

# RAG embedder + reranker (CPU-only user services)
alias ragstart='systemctl --user start rag-embedding rag-reranker'
alias ragstop='systemctl --user stop rag-reranker rag-embedding'
alias raglogs='journalctl --user -u rag-embedding.service -u rag-reranker.service -f'
```

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
