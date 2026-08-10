# Recommended models

Models JayNet's docs and scripts point at. Two rules for this list: the
license must permit redistribution and commercial use (so I *could* ship or
mirror weights alongside the project), and an official or well-maintained
GGUF must exist on HuggingFace. Verify the license in the model repo before
mirroring anything — licenses occasionally change between revisions.

## The default model set (ships as the preset seed)

What a fresh install is configured for out of the box — code fallbacks,
shipped presets and `quickstart.sh` all point here. All Apache-2.0, all
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
| **Qwen3-4B** (Instruct-2507) | ~2.5 GB | Apache-2.0 | Default pick: strong for its size, thinking toggle, official GGUFs (`Qwen/Qwen3-4B-GGUF`) |
| **Qwen3-1.7B** | ~1.3 GB | Apache-2.0 | The CPU try-out when 4B crawls: same family/template, tool calling intact (`Qwen/Qwen3-1.7B-GGUF`) |
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
