---
name: gpu-serve
description: Launch or manage a model / embedding server on a specific GPU on this workstation (dual Radeon R9700, ROCm). Load when asked to serve a model, free VRAM, run an embedder/reranker, or put work on GPU 1.
---
# Serving models on this box (dual R9700, ROCm 7, gfx1201)

This workstation has two AMD Radeon AI PRO R9700 (RDNA4 / gfx1201, 32 GB each).
GPU 0 runs the orchestrator brain (Qwen3 MoE on llama-server :8090, behind LiteLLM
:4000). **Keep GPU 0 for the brain; put new work on GPU 1.** Use the `serve.*`
tools to manage model servers; only drop to a raw `job.start` for something serve
doesn't cover.

## Step 1 — check headroom first

Always `gpu.status` before launching. Each card is 32 GB; confirm GPU 1 has room
for what you're about to load (a 7–14B quant ≈ 8–16 GB; an embedder/reranker is
small). `serve.start` also does an advisory VRAM check, but look first.

## Step 2 — launch with serve.start (pinned to GPU 1)

`serve.start` pins to GPU 1 by default, sets `GPU_MAX_HW_QUEUES=1`, sources
`rdna4-env.sh`, picks a free port, and waits until the server answers:

    serve.start(name="fast-llm", preset="<preset>", kind="llm", est_vram_gib=12)

- Presets come from the preset catalog (seeded from the repo's `presets/` dir,
  edited in admin → Presets), models live in `$ORCH_MODELS`; the serve
  dispatcher resolves the preset (give an explicit `command` only for something
  the dispatcher can't express).
- The brain owns 8090 and LiteLLM 4000; serve auto-avoids them.
- Do **not** set `ROCR_VISIBLE_DEVICES` — this setup uses `HIP_VISIBLE_DEVICES`
  alone (serve does this for you), or the masks fight and the device won't bind.

## Step 3 — use it

- For `kind="llm"`, serve registers a LiteLLM alias (the server's `name`) so you
  can immediately `agent.spawn(model="fast-llm", task=...)` or `llm.call` against
  it — a second local model on GPU 1 is exactly what lets a spawned sub-agent run
  in parallel with the brain. If registration fails, the result tells you the
  direct `…/v1` URL.
- `serve.status` / `serve.health` confirm it's live; re-check `gpu.status` to
  confirm it landed on GPU 1.

## RAG embedder / reranker

`rag.*` calls an embedding/rerank server via `tools.rag.embed_url` /
`rerank_url`. Launch one with `serve.start(name="embedder", kind="embedding",
wire_rag=true)` (or `kind="rerank"`) — `wire_rag` points `rag.*` at it for the rest
of the session (set it in config to persist). Both are small and can stay resident
alongside one medium model.

## Gotchas

- 32 GB per card is the hard limit — embed + rerank (small, resident) **plus** one
  medium model or one job is realistic; a second 35B won't fit.
- If `gpu.status` errors with "no GPU tool", the service PATH is missing
  `/opt/rocm/bin` (and check the user is in the `render`/`video` groups).
- Stop a server with `serve.stop(<name>)` to free its VRAM before launching
  something bigger (it also deregisters the LiteLLM alias).
