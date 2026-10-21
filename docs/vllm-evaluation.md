# vLLM evaluation — hot-swap vs the JayNet preset swapper

*Question parked from the backlog: can vLLM replace or improve the
llama.cpp-based model swapping JayNet is built on? Evaluated 2026-10-19
against vLLM's current feature set and the R9700 (gfx1201, ROCm 7.2.4)
target machine.*

*Update 2026-10-21: backend-typed presets (shipped after this evaluation)
now let JayNet launch vLLM itself — a preset whose binary has registry
type `vllm` hands off to `runtime/launch_argv.py` (see
[models.md](models.md#backend-typed-presets)). The adoption-only framing
below is historical. The sleep-aware swapper follow-up (sleep/wake
instead of start/stop for vLLM-backed presets) is still open.*

## Verdict (short)

Keep the llama.cpp swapper as the core. vLLM earns an optional seat as an
**adopted external server** for the one workload it is genuinely better at —
a high-throughput brain served from safetensors weights — and the helper
script can now build it for gfx1201 (`scripts/build_tools.sh vllm rocm`).
No JayNet code changes are needed to use it that way: point a preset at the
vLLM server's URL (see `docs/models.md` — "Adopting a server that's already
running").

## What vLLM actually offers for swapping

vLLM has no multi-model hot-swap in one server. What exists
([sleep mode docs](https://docs.vllm.ai/en/latest/features/sleep_mode/)):

- **Sleep level 1**: weights offloaded to CPU RAM, KV cache discarded, server
  stays up. Wake in seconds, same model. HTTP: `POST /sleep?level=1`,
  `POST /wake_up` (requires `VLLM_SERVER_DEV_MODE=1` +
  `--enable-sleep-mode`). Works on ROCm.
- **Sleep level 2**: weights *and* KV discarded — wake to *updated* weights
  of the same architecture (`collective_rpc("reload_weights")`, built for
  RLHF). Not a general model switcher.
- Multi-model in practice = **one server per model**, sleeping servers held
  in CPU RAM (e.g. LLMSnap-style wrappers). Swap = sleep current + wake
  target — seconds instead of a process restart and weight load.

So the honest comparison with JayNet's swapper:

| | llama.cpp swap (today) | vLLM sleep-mode pool |
|---|---|---|
| Swap latency | process restart + weight load (tens of s) | seconds (wake from RAM) |
| Idle cost | zero — stopped processes hold nothing | every sleeping model's weights sit in CPU RAM |
| Model format | GGUF — JayNet's whole preset library | safetensors (GGUF is experimental, single-file only) |
| Ops weight | one static binary per backend | full torch stack, 20–40 min source build, dev-mode endpoints |
| Concurrency | serial per server | continuous batching — wins when runs overlap |
| Measured placement | presets carry measured VRAM; swapper schedules | vLLM wants a fixed `--gpu-memory-utilization` slice per server |

The swap-latency win is real but only pays when swaps are frequent AND the
RAM to park sleeping models is free. JayNet's swapper already knows each
preset's measured VRAM and frees slots on demand; a vLLM pool parks that
same memory in RAM permanently, per model.

## Where vLLM would win for us

- **Throughput under overlap**: spawn/delegate/sub-agents can run model
  calls concurrently; llama.cpp serializes per server, vLLM batches. A busy
  brain is the one place this shows up in wall-clock.
- **Speculative decoding**: MTP/Eagle3 run on gfx1201 via the ROCM_ATTN
  backend ([field notes](https://gist.github.com/dobriak/9dff293e5ea6ffc9f4258bf52c3b5333))
  — relevant for the MTP quants we already test under llama.cpp.
- **FP8 on RDNA4**: gfx1201 supports `float8_e4m3fn`; vLLM's ROCm FP8 path
  is active there.

## Why not wholesale

- **GGUF library**: presets, quants, mmproj files, the downloader — the
  whole model zoo is GGUF. vLLM would fork the library into a second
  safetensors collection at ~2x disk.
- **Batch-1 reality**: most JayNet runs are single-stream; llama.cpp is
  competitive at batch-1 decode and far lighter to operate.
- **Single binary vs platform**: the swapper manages one static binary per
  backend; vLLM is a torch-platform server with dev-mode gates on exactly
  the endpoints (sleep/wake) we would depend on.

## Try-it path (what the build is for)

1. `./scripts/build_tools.sh vllm rocm` — source build for gfx1201
   (ROCm torch wheels first, `--no-build-isolation`, PYTORCH_ROCM_ARCH).
2. Serve the brain's HF weights with sleep mode armed:
   `VLLM_SERVER_DEV_MODE=1 vllm serve <hf-model> --enable-sleep-mode --port 8000`
3. Adopt it as a preset (`backend: vllm`, api_base `http://localhost:8000`)
   and A/B tok/s, TTFT and cache behavior against the same model under
   llama.cpp ROCm on the same prompts (`scripts/route_bench.py`).

Decision after the measurement, not before. If the brain gains meaningfully,
the next question is a sleep-aware slot in the swapper (sleep/wake instead
of start/stop for vLLM-backed presets) — that is a follow-up, not part of
this evaluation.
