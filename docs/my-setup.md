# My setup (wolf) — the author's box, example only

*Operators looking for a worked example — this is the author's personal
daily driver, NOT a recommendation or a default. The shipped factory seed
is the generic brain/specialist teaching pair; your hardware and models
will differ — that's the point of the preset catalog
([model-placement.md](model-placement.md), [models.md](models.md)).*

One workstation doing everything:

- **Hardware:** AMD Ryzen 9 7950X (16C/32T), 64 GB RAM,
  2× AMD Radeon AI PRO R9700 32 GB (RDNA4, ROCm), 2× 1 TB NVMe
  (models and data on separate disks)
- **Models:** brain = Cyber-Tiel-Coder-35B-A3B (UD-Q4_K_M + MTP, the
  peculiar-ragdoll build) on GPU 0 @262k ctx — a routing-trained MoE with
  ~3B active params at ~106 tok/s, vision included via its BF16 mmproj.
  It won the brain slot in the [bakeoff](brain-bakeoff.md): 38/41
  with the first Terminal-Bench clean sweep and 81% voluntary delegation
  — the "models won't delegate" problem solving itself. Specialist =
  Qwen3.8-27B Turbo NEO-CODER Q4_K_M dense (MTP) on GPU 1 @262k ctx — the
  `specialist.delegate` target and allround worker, and the vision
  endpoint (mmproj — no separate vision model). Swap-in alternates:
  Hemmingway-1 (creative writing) and Helcyon-Solara-2-14B (chatting) on
  the specialist slot, Dolphin-3.0-8B (security). Reference points from
  the search: Ternary-Bonsai-2-27B the previous champion (32/41 — 27B
  mass at ~9 GB VRAM, but ~32 t/s), the Spark-X2.5-4B MoE the speed-era
  routing-discipline benchmark, qwen35-9B the raw-speed record at 80 t/s.
  Brain and specialist candidates were
  picked by eval, not vibes — the full comparison is in
  [brain-bakeoff.md](brain-bakeoff.md). Embed (Qwen3-Embedding-8B),
  rerank (Qwen3-Reranker-0.6B) and Whisper large-v3-turbo (STT) on CPU
- **Stack:** llama.cpp self-built (ROCm + Vulkan), LiteLLM proxy, web
  console — all systemd user services; the process manager supervises the
  model servers
- **Around it:** nginx + Let's Encrypt on a separate host, a SearXNG
  container for web search, cloud models (kimi, glm, gemini, qwen) as
  approval-gated escalation only

Why it matters when reading other docs: the eval numbers in
[brain-bakeoff.md](brain-bakeoff.md) and
[clm-bakeoff.md](clm-bakeoff.md), and the hardware notes in
[llama-ops.md](llama-ops.md) (dual 32 GB RDNA4 worked examples, the ROCm
pinning gotcha), were measured on this box. The shipped `config/` prompt
also names these models/cloud aliases — edit it or point
`orchestrator.system_prompt` at your own (see
[manual_installation.md](manual_installation.md)).
