# Running the model servers (llama.cpp operations)

JayNet treats `llama-server` as managed infrastructure: every model is a
**preset** (Admin → Presets, catalog DB seeded from `config/runtime.yaml`),
launched by `scripts/start-model.sh` from a `.conf` file in `presets/`, and
supervised by the web service's process manager. This doc explains the knobs
inside those `.conf` files, the VRAM math behind them, and what to do when a
server misbehaves. Which models to pick lives in
[models.md](models.md); which card runs what in
[model-placement.md](model-placement.md).

See the resolved launch command for any preset without starting anything:

```bash
scripts/start-model.sh brain --dry-run        # catalog preset by name
scripts/start-model.sh --preset presets/tess-4-27b-q6_k.conf --dry-run
```

## The knobs that matter

Each key in a preset `.conf` maps to a llama-server flag and a real
trade-off:

| `.conf` key | Flag | What it controls |
|---|---|---|
| `MODEL_PATH` | `-m` | The GGUF on disk. Whatever is there is what loads — no downloads, no cache layer. |
| `CTX_SIZE` | `-c` | Context window in tokens. KV cache VRAM scales linearly with this; double context ≈ double cache. |
| `GPU_LAYERS` | `-ngl` | Layers offloaded to GPU. `99`/`999` = all. Lower only if the model doesn't fit and you accept CPU-slow inference. |
| `SPLIT_MODE` | `--split-mode` | `layer` (default here): whole layers per GPU, no inter-GPU traffic during decode — right for bandwidth-limited cards. `row` parallelizes batches but hurts decode on AMD. `none` ignores extra GPUs. |
| `TENSOR_SPLIT` | `--tensor-split` | Proportions across cards (`1,1` equal, `3,1` = 75/25). |
| `VISIBLE_DEVICES` | `HIP_VISIBLE_DEVICES` | Which cards the process may see at all. The launcher exports this **alone** — see the AMD gotcha below. |
| `CACHE_TYPE_K/V` | `--cache-type-k/v` | KV cache quantization. `q8_0` is essentially free in quality and halves cache VRAM vs FP16; turn on for any context > 8k. |
| `FLASH_ATTN` | `--flash-attn` | Faster prefill, smaller footprint on long context. Keep `on` unless an old GPU crashes with it. |
| `BATCH_SIZE` / `UBATCH_SIZE` | `--batch-size` / `--ubatch-size` | Prefill batching. 2048/512 is a sane default; bigger = faster long-prompt prefill, more VRAM. |
| `JINJA` | `--jinja` | Use the chat template embedded in the GGUF. **Always on for chat/tool-call workloads** — without it the generic format won't match what the model was trained on. |
| `TOOLS_TEMPLATE` | `--chat-template-file` | Override the embedded template; only for debugging tool-call rendering. |
| `TEMP`, `TOP_K`, `TOP_P`, … | sampling | Generation personality. Brains run cool (`TEMP` ~0.6–0.7). |
| `EXTRA_ARGS` | — | Escape hatch for anything else llama-server accepts. |

Not every key is a launch flag — three classes to tell apart before adding
one:

- **Slot keys** (`PORT`, `HOST`, `ALIAS`, `VISIBLE_DEVICES`, `LLAMA_BIN`,
  `DEVICE_ENV`) are captured by the launcher but applied **only** in
  `--preset FILE` mode (what `serve.*` uses); in catalog/name mode the
  catalog row owns the slot and these are ignored.
- **`BACKEND`** is display metadata for the Models page (`rocm`, `cuda`,
  `cpu`, …) — the launcher never reads it.
- **Unknown keys are silently dropped.** `SYSTEM_PROMPT` is the classic
  trap: llama-server has no such flag and the launcher intentionally
  ignores it. If a key isn't in the table or the slot list above, it does
  nothing.

## Creating and editing presets

A preset = one catalog row (name, role, alias, port, device, binary,
`served_id`, VRAM estimate, strength tags) + a `.conf` with the launch flags
above. The full lifecycle, without leaving the console:

1. **Get the weights** — `scripts/pull-model <org/repo>` downloads a GGUF
   into your models dir (verify the SHA-256 against the uploader's hash).
2. **Admin → Presets → + new preset** — fill the row fields and the `.conf`
   textarea (or edit an existing row; conf edits apply on next launch).
3. **Save** — the catalog DB stores the row and materializes the `.conf`.
   The catalog is seeded from `config/runtime.yaml` on first use; afterwards
   the DB wins (delete `presets.db` in the data dir to re-seed from yaml).
4. **Boot model slots** (same tab) — which preset each managed process boots
   by default; relaunch the process from Admin → Processes to apply.

Three fields are contracts, not labels:

- **alias + port** must match a static entry in `config/litellm.yaml` — the
  proxy is stateless, so reachability comes from that static alias, not from
  runtime registration.
- **served_id** must equal the `--alias` the `.conf` launches with; it's how
  JayNet detects a wrong model on a slot.
- **strengths** are machine-readable tags surfaced to the brain, which uses
  them to pick a specialist mid-chat (`model.use`).

Device decides placement: one GPU, all GPUs (layer-split), or CPU — two
models may share a card if their combined VRAM fits (the math below).

The same tab edits **cloud models** — the `llm.call` escalation path. A row
stores the key's env-var *name* (the pill shows whether it's set in
`~/.config/jaynet.env`; secrets never enter the catalog), $/1M tokens
in/out (drives cost accounting), a thinking default, fallbacks, and the role
text shown to the brain. Saving re-renders the proxy config — the repo's
`litellm.yaml` stays the pristine seed — and disabled rows stay in the DB
but aren't served.

## VRAM math

```
total_vram ≈ weights + kv_cache + activations(~1–2 GB) + overhead(~0.5 GB)

weights  = params × bytes_per_param      (Q4 ≈ 0.6–0.7, IQ4_XS ≈ 0.55, Q8 ≈ 1.0)
kv_cache = f(ctx_size, layers, hidden)   quantize it (q8_0) to ~halve
```

Worked examples, dual 32 GB RDNA4:

| Model | Quant | Weights | + 32k q8_0 KV | Fits |
|---|---|---|---|---|---|
| Qwen3-4B | Q4_K_M | ~2.5 GB | ~1 GB | one card, trivially |
| 27B dense | IQ4_NL/Q6_K | 16–20 GB | ~6 GB | one 32 GB card |
| 35B-A3B MoE | Q4–Q6 | 20–26 GB | ~6 GB | one 32 GB card |
| 70B dense | Q4_K_M | ~40 GB | ~8 GB | layer-split `1,1` |

Monitor with `rocm-smi` or `radeontop` (`nvidia-smi` doesn't exist on AMD).
If a server OOMs mid-request: lower `CTX_SIZE` or quantize the KV cache
harder — or something else grabbed VRAM; `rocm-smi` shows what's resident.

## The AMD GPU-pinning gotcha (RDNA4 / ROCm)

Set `HIP_VISIBLE_DEVICES` **alone** and leave `ROCR_VISIBLE_DEVICES` unset.
Setting both double-filters the device list and the model silently lands on
CPU (the card shows 0% in `rocm-smi` while the host swaps). The launcher
handles this for you; if you ever hand-roll a service file, this is the line
that bites.

## Swapping and stacking models

The swapper's reason to exist: a box that cannot hold every model at once
can still *use* many of them. One slot hosts the finetuned experts — a
coding finetune, a security finetune, a research finetune — and `model.use`
loads whichever the current task needs in place of the previous one.

- **Swap a slot:** `model.use('<preset>', swap: true)` (the brain does this
  itself mid-chat; Admin → Presets shows what's live per GPU and free VRAM).
  A different model on the target slot is reported, not evicted, unless
  `swap: true` — and never a systemd-served one.
- **Two brains at once:** register two brain servers under the same
  `local-orchestrator` alias in `config/litellm.yaml` with
  `routing_strategy: simple-shuffle` — the proxy round-robins, and with
  `local_concurrency` raised, fan-out sub-agents genuinely overlap.
- The old "one systemd unit per model on its own port" pattern still works
  for anything outside the catalog; [llama-swap](https://github.com/mostlygeek/llama-swap)
  is the external option once you juggle many models JayNet doesn't own.

## Benchmarking

`llama-bench` ships with the build:

```bash
llama-bench -m <model.gguf> -ngl 99 -fa 1 -ctk q8_0 -ctv q8_0 -p 512,1024 -n 128
```

Ballpark on a 32 GB RDNA4 card (Vulkan/ROCm, 2026): a 30B-class MoE at 4-bit
decodes ~100+ tok/s; a 27B dense ~25–40; a 70B split over two cards ~12–20.
If you're far below: flash-attn off, KV cache unquantized, or layers left on
CPU are the usual culprits.

## When a server misbehaves

1. **Won't start** — Admin → Processes shows the card red; open its log
   there or `journalctl --user -u jaynet-web -e`. Check the model path
   exists and `--dry-run` output looks right.
2. **`/v1/models` empty / wrong id** — corrupted GGUF (re-verify SHA-256) or
   a GGUF newer than the llama.cpp build ("unsupported GGUF version").
3. **Garbage output** — wrong chat template (drop `TOOLS_TEMPLATE`, keep
   `JINJA=yes`), or a quant too aggressive for the model size.
4. **Slow first request after (re)start** — normal: the system prompt
   prefills into the KV cache once; subsequent calls reuse it.
5. **Tool-call JSON malformed** — the model isn't tool-call trained, or the
   embedded template predates the format; override with a known-good
   `TOOLS_TEMPLATE`.
6. **Throughput decays over time** — restart the server; if it recurs, look
   for VRAM neighbors or host load (`htop`).

## Further reading

- llama.cpp `tools/server/README.md` — every flag and endpoint.
- llama.cpp `tools/quantize/README.md` — quantize your own from F16.
- HuggingFace GGUF viewer — see what's inside a file.
- [LEARNING_GUIDE.md](../LEARNING_GUIDE.md) §3.4 — the one-paragraph theory
  version of this doc.
