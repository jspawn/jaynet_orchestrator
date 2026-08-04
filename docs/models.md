# Recommended models

Models JayNet's docs and scripts point at. Two rules for this list: the
license must permit redistribution and commercial use (so we *could* ship or
mirror weights alongside the project), and an official or well-maintained
GGUF must exist on HuggingFace. Verify the license in the model repo before
mirroring anything — licenses occasionally change between revisions.

## Quick start / CPU-small (brain, one-model installs)

| Model | Size (Q4) | License | Why |
|---|---|---|---|
| **Qwen3-4B** (Instruct-2507) | ~2.5 GB | Apache-2.0 | Default pick: strong for its size, thinking toggle, official GGUFs (`Qwen/Qwen3-4B-GGUF`) |
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
