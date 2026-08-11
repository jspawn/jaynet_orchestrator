# Recommended models

Models JayNet's docs and scripts point at. Two rules for this list: the
license must permit redistribution and commercial use (so I *could* ship or
mirror weights alongside the project), and an official or well-maintained
GGUF must exist on HuggingFace. Verify the license in the model repo before
mirroring anything — licenses occasionally change between revisions.

## The default model set (ships as the preset seed)

What a fresh full install is configured for out of the box — code fallbacks
and shipped presets point here (`quickstart.sh` grabs the smaller Qwen3-1.7B
instead: it is the CPU try-out). All Apache-2.0, all
with official GGUFs, downloadable via `scripts/pull-model`:

| Role | Model | Wired as | Port | Runs on |
|---|---|---|---|---|
| brain | **Qwen3-4B** (`Qwen/Qwen3-4B-GGUF`) | LiteLLM alias `local-orchestrator` | 8090 | CPU or any 8 GB GPU |
| embed | **Qwen3-Embedding-0.6B** | direct `embed_url` (`:8095/v1/embeddings`) | 8095 | CPU |
| rerank | **Qwen3-Reranker-0.6B** | direct `rerank_url` (`:8096/v1/rerank`) | 8096 | CPU |

Upgrade path when hardware allows: swap the brain for Qwen3-32B /
Qwen3-30B-A3B (or add a specialist slot) in admin → Presets — the aliases
stay, so nothing else changes.


## Quick start / CPU-small (brain, one-model installs)

| Model | Size (Q4) | License | Why |
|---|---|---|---|
| **Qwen3-1.7B** | ~1.3 GB | Apache-2.0 | **Quickstart default**: fast on CPU, same family/template as the 4B, tool calling intact (`Qwen/Qwen3-1.7B-GGUF`) |
| **Qwen3-4B** (Instruct-2507) | ~2.5 GB | Apache-2.0 | Stronger still-small brain; the preset-seed default for full installs, thinking toggle (`Qwen/Qwen3-4B-GGUF`) |
| Phi-4-mini-instruct | ~2.5 GB | MIT | Microsoft's small reasoner; MIT is the cleanest license in class |
| SmolLM3-3B | ~2 GB | Apache-2.0 | HuggingFace's own, fully open (weights + data + recipe) |
| DeepSeek-R1-Distill-Qwen-7B | ~4.5 GB | MIT | When you want visible reasoning on modest hardware |

Avoid as shipped defaults (restrictive licenses, fine for personal use):
Llama-* (Meta Community License), Gemma-* (Gemma Terms), Mistral Ministral
(MRL, non-commercial). Mistral-7B (Apache-2.0) is the exception in that
family.

## Full setup (GPU brain / specialist)

Any model you like — presets are per-host. License-clean defaults if you
mirror or redistribute:

- **Brain:** Qwen3-32B / Qwen3-30B-A3B (MoE, fast) — Apache-2.0
- **Specialist (coding):** Qwen3-Coder-30B-A3B — Apache-2.0
- **Specialist (reasoning):** DeepSeek-R1-Distill-Qwen-32B — MIT

## Embed + rerank (RAG tools)

| Role | Model | License |
|---|---|---|
| embed | Qwen3-Embedding-0.6B / -4B | Apache-2.0 |
| embed (alt) | bge-m3 | MIT |
| rerank | Qwen3-Reranker-0.6B | Apache-2.0 |
| rerank (alt) | bge-reranker-v2-m3 | MIT |

All four run fine on CPU as GGUF — matching JayNet's "embed/rerank stay off
the GPU" posture.

<a name="adopt-existing-server"></a>
## Adopting a server that's already running (vLLM / Ollama / …)

JayNet launches llama.cpp itself, but any OpenAI-compatible server you already
have running can be adopted as a **remote preset** (admin → Presets → edit →
"remote"): JayNet health-probes it, routes slots/aliases to it through the
proxy, and never launches or stops anything off-box.

- **Endpoint**: a bare host (`192.168.1.50`, port from the preset's port
  field) or a full URL (`http://vllm-box:8000`, `http://ollama-box:11434`,
  `https://models.example.com` — scheme default ports work too).
- **Backend**: `llama` (default), `vllm`, `ollama`, `openai`. Anything but
  llama loses the llama-only extras (jinja thinking switch, llamacpp
  metrics) unless you opt in under **capabilities**:
- **Capabilities**: `vision` / `thinking` overrides. Auto = llama defaults
  (thinking switch on, vision off). For a vision-capable vLLM server set
  vision on; for an Ollama model whose template honors `enable_thinking`
  set thinking on.
- **served_id** must match an id the server reports on `/v1/models`
  (multi-model servers like Ollama list several — the preset matches its own
  id among them; e.g. Ollama's `qwen3:4b`).

Ollama quick example: preset `remote_host: http://ollama-box:11434`,
`backend: ollama`, `served_id: qwen3:4b`, assign it to a slot — done.

Security note: adopted endpoints are plain LAN HTTP unless you put TLS in
front — JayNet sends chat content there, so keep them on your network.
Adopted endpoints must not require an API key (per-preset key support is
parked for the managed-backend layer) — a keyed endpoint is reported as
"authentication required" by `model.use`/`model.list`, not adopted.

## External embed / rerank endpoints

The RAG tools talk to plain URLs (`tools.rag.embed_url` /
`tools.rag.rerank_url` in `config/runtime.yaml`) — they don't have to be the
local llama-servers. Point them at any compatible service:

- **embed_url**: anything OpenAI-`/v1/embeddings`-compatible (vLLM, TEI,
  Infinity, Ollama). Set `embed_model` to the id that server expects.
- **rerank_url**: llama.cpp's `/v1/rerank` shape, but the parser also
  tolerates Cohere/Jina-style responses — TEI (`/rerank`), Infinity, or a
  hosted Cohere endpoint all work.

Handy for low-memory installs: run embed+rerank on a small box (or a hosted
endpoint) and keep the GPU box for the chat models.
