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
- **Stack:** llama.cpp self-built (ROCm + CPU, HEAD + three local shutdown
  patches) and the radiance C++/HIP server, all built by
  `scripts/build_tools.sh` into versioned prefixes under
  `/srv/jaynet-bin`; LiteLLM proxy + web console as systemd user services;
  the process manager supervises the model servers
- **Around it:** nginx + Let's Encrypt on a separate host, a SearXNG
  container for web search, cloud models (kimi, glm, gemini, qwen) as
  approval-gated escalation only

## Boot slots (what runs all day)

| slot | preset | engine | where | ctx | measured |
|---|---|---|---|---|---|
| brain | occamy-1-0 (35B-A3B MoE, APEX-I-MiniPlus + Q8_0 MTP draft) | llama.cpp ROCm | GPU 0, :8090 | 262k | 20.9 GiB VRAM / 1.9 GiB RAM |
| specialist | qwen38-radiance (Qwen3.8-27B MXFP4 + DFlash2) | radiance | GPU 1, :8079 | 32k | 29.6 GiB VRAM / 10.5 GiB RAM |
| embed | Qwen3-Embedding-8B Q8_0 | llama.cpp CPU | :8095 | — | — |
| rerank | Qwen3-Reranker-0.6B Q8_0 | llama.cpp CPU | :8096 | — | — |
| stt | Whisper large-v3-turbo | whisper.cpp CPU | :8099 | — | — |
| vision | vision-qwen3-vl-8b (Q4_K_M + F16 mmproj) | llama.cpp CPU | :8098 | 8k | — |

Vision deliberately runs on CPU: an 8k-ctx helper answers image questions
without evicting a GPU resident. specialist2/specialist3 slots exist but
are unassigned.

## Swap-in presets (same slot, one at a time)

All on the specialist slot (GPU 1, :8080) unless noted:

- **qwen3-8-27b-turbo-coder** — llama.cpp coding allrounder (Q4_K_M, MTP,
  dual-GPU tensor split 0+1), the pre-radiance specialist; 29.7 GiB.
  Strengths: allround, coding, multi-step, research, uncensored
- **bugtrace-26b** — Gemma-4 26B-A4B MoE bug hunter (+ F16 mmproj);
  21.9 GiB. Strength: bugtrace
- **redcell-26b-osint** — Gemma-4 26B-A4B MoE OSINT/CTI (+ BF16 mmproj);
  25.1 GiB. Strength: osint
- **helcyon-solara-2-14b** — companion chat/roleplay; 18.6 GiB.
  Strength: chatting
- **orca-27b** — security/pentest; 24.1 GiB. Strengths: security,
  uncensored
- **hemmingway-1-q6k** — creative/everyday writing (:8093);
  CC BY-NC. Strength: creative
- **gemma-4-31b-vision** — GPU vision alternative for the vision slot
  (32k ctx; 21.0 GiB) when the CPU helper is too weak

## Strengths registry

Registered tags (shown in the brain's system prompt, routed on by
`specialist.delegate` / `agent.spawn`): **coding** (synthesis, debugging,
refactors, test fixing), **research**, **reasoning**, **security**,
**vision**, **multi-step**, **creative**, **osint** (source grading,
attribution, geolocation, passive recon), **bugtrace** (stack traces,
log forensics, crash triage). `allround` is the built-in catch-all.

## Numbers worth knowing

- radiance specialist vs the same model class on tuned llama.cpp:
  decode **94–98 vs 39–40 tok/s (≈2.4×)**, prefill **~3.4k vs ~475 tok/s
  (≈7×)** — that gap is why the specialist slot runs radiance now
- the brain (MoE, MTP draft) idles at ~21 GiB on GPU 0, leaving headroom;
  the radiance specialist pins 29.6 GiB on GPU 1 — co-tenancy on that
  card is RAM-gated by the swapper, not guessed
- all footprints above are measured (`Admin → Models → Presets →
  measure`), not model-card arithmetic

Why it matters when reading other docs: the hardware notes in
[llama-ops.md](llama-ops.md) (dual 32 GB RDNA4 worked examples, the ROCm
pinning gotcha) were measured on this box. The shipped `config/` prompt
also names these models/cloud aliases — edit it or point
`orchestrator.system_prompt` at your own (see
[manual_installation.md](manual_installation.md)).
