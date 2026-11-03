# Watchlist

External models, papers, and runtimes worth revisiting when their blockers
change. Actionable JayNet work stays in `ToDos_for_later.md`.

## Zyphra Hierarchical Attention (ZHA) — added 2026-11-03

[Paper](https://arxiv.org/abs/2610.42112): hierarchical sparse attention over
a tree-indexed KV cache. Especially relevant because its main conversion
experiment uses **Qwen3.6-35B-A3B**, Occamy's base architecture.

- Frozen backbone; only small reference-query routers are distilled on about
  1B tokens. Reasoning, agentic coding, and retrieval stay close to dense
  attention in the reported experiments.
- Empirical quality results reach 256K context. Larger speedups and context
  lengths are model-based extrapolations, not measured serving results.
- **Blockers:** no optimized kernels or drop-in llama.cpp/radiance/vLLM
  support. Converting Occamy would need distillation plus a compatible
  serving runtime.
- **Revisit when** a runnable Qwen3.6/Occamy-compatible checkpoint and
  backend land. Then A/B quality, TTFT, decode speed, VRAM, and compaction
  frequency. Do not build a custom attention runtime for this.

## Brain watch: RecursiveSI/flint (evaluated on paper 2026-10-22, parked)

[Flint](https://huggingface.co/RecursiveSI/flint) — 39.8B-total/~2.5B-active
MoE trained from scratch, Apache-2.0, 262k context (35 of 49 layers are
512-token sliding window), aimed at **coding agents** (TB 2.1 54.9,
SWE-bench Verified 74.2, SWE-Pro 89.7). Compared against occamy-1.0 as a
next brain and parked:

- **Wrong role on paper.** Flint is specialist-shaped (repo digging, bug
  fixes, tests); the brain slot wants occamy's co-work/long-horizon
  training (state tracking across delegation + compaction). Occamy's card
  even posts the higher TB 2.1 (59.0 vs 54.9); only occamy publishes
  tool-calling/co-work benches.
- **Nothing to serve it with.** Custom arch (NoPE full-attention layers,
  attention sinks, per-head QK RMSNorm): no GGUF, no llama.cpp support,
  official path is SGLang on B300/H200; quants announced for NVIDIA +
  Apple Silicon only. Can't load it on the R9700 stack today.
- **Revisit when** llama.cpp/GGUF support lands AND someone has it running
  on ROCm — then evaluate it as a *specialist* candidate (radiance slot
  class), not as brain. Check the HF repo + llama.cpp issues for "flint"
  in a few weeks.

## Research watch: latent/parallel generation (evaluated 2026-10-21, skip for now)

Two Together/Stanford papers, both reviewed and parked — elegant, but
nothing servable today:

- **HybridAgent** ([paper](https://arxiv.org/pdf/2610.18861),
  [model](https://huggingface.co/togethercomputer/HybridAgent-14B-Agentic),
  [code](https://github.com/togethercomputer/HybridAgent)) — Qwen3-Base +
  HybridLink adapter; thinks in latent space (hidden states fed back as
  inputs), acts in text. +17.4% over agentic baselines, ~2× faster, ~50%
  fewer tokens. **Blocker:** no serving engine supports hidden-state
  feedback; backbone-only serving loses the entire point. Revisit if
  latent/hybrid generation lands in vllm or llama.cpp upstream.
- **NCM — Next-Concept Modeling**
  ([paper](https://arxiv.org/pdf/2610.14162)) — multiscale VQ-VAE codes,
  generates coarse-to-fine with all codes of a level in parallel ("whole
  sentences at once", 10–31× faster than diffusion LMs). **Blocker:** a
  training recipe, not a checkpoint — experiments at ~100M params, nothing
  to download or serve.

Watch instead (could arrive as a serving feature we'd get for a preset
flag): **DFlash**-style diffusion drafters for speculative decoding, and
production diffusion LMs (**Mercury**, **DiffusionGemma**) reaching
llama.cpp/vllm support.

## Open intent-routing checkpoint for Jev

Current decision: stay with keyword routing. Hosted cloud routing sends
every request's text off-box, the open 2B checkpoint underperformed the
keyword router, and the 9B does not fit the current layout.

**Revisit when** an open checkpoint trained specifically for intent routing
lands (Open-Jev 27B run / V3 stage). See "Decision model" in
`ToDos_for_later.md` for the shipped plugin and CLM context.
