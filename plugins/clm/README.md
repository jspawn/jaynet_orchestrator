# CLM plugin — Contrastive Language Model decisions

CLM-8B ([Contrastive-LM/CLM](https://github.com/Contrastive-LM/CLM),
Apache-2.0) is a *System One* decision model: state + typed questions in,
calibrated probabilities out — one embedding plus a dot product per
candidate, **no text generation**. It matches Jev on tool-calling tasks at
up to 9× lower latency, and fine-tuned as a verifier it holds SOTA on
Terminal-Bench 2.1 (87.6%) and DeepSWE (81.6%).

This plugin is the **successor of the jev/jevify delegation-classifier
experiment** (same wire contract, purpose-built model): jevify borrowed the
resident specialist as an improvised generative judge and paid its full
latency per classification; CLM is the dedicated instrument for that job
and leaves the specialist alone.

What it provides:

- **`clm.decide`** — typed questions (choice / noul / score) with
  calibrated probabilities.
- **`clm.rank`** — order free-form candidates (best-of-N, next moves,
  draft answers), best first.
- **`route_request` hook (opt-in)** — classifies each incoming request
  into a strength tag for delegation routing. Ships **off**; keyword
  routing stays the default until the A/B proves itself.

## What else needs installing (the sidecar pair)

The plugin itself is stdlib-only. The model is two processes:

**1. Qwen3-8B pooling encoder** (the CLM heads are encoder-locked — no
other model works):

- Download `Qwen/Qwen3-8B-GGUF` Q4_K_M (≈5 GB) via admin → Presets →
  Download from HuggingFace.
- Register `presets/embed-qwen3-8b-clm.conf` (repo seed) as a preset —
  it serves `/v1/embeddings` with last-token pooling on `127.0.0.1:8094`,
  ~5.5 GB VRAM, fits beside the resident brain+specialist. CPU fallback
  notes are in the conf.

**2. `clm-serve`** (the decision API on :8700):

```bash
cd /path/to/jaynet
# uv + CPU-only torch + no-deps: contrastive-lm otherwise drags in vllm and
# ~3 GB of nvidia CUDA wheels, and the clm package never imports vllm when
# the encoder is llama.cpp (checked: fastapi/uvicorn/torch/numpy/requests/
# huggingface_hub only). The lean venv is ~850 MB.
uv venv clmenv
uv pip install --python clmenv/bin/python torch --index-url https://download.pytorch.org/whl/cpu
uv pip install --python clmenv/bin/python --no-deps contrastive-lm
uv pip install --python clmenv/bin/python fastapi uvicorn numpy requests huggingface-hub
# systemd user unit template: systemd/clm-serve.service
systemctl --user enable --now clm-serve
```

Measured on CPU (Q8_0 encoder, 16 threads): a warm typed decision answers
in **~0.3 s** — clm-serve caches state/action vectors, so repeat candidate
sets skip the encoder entirely. Calibration caveat: zero-shot CLM on a
custom candidate set is decent but not authoritative (a geography question
misrouted at 0.31 confidence) — the `route_threshold` (0.6) absorbs that,
and it's why the hook ships off until the A/B + a fine-tuned head.

Sanity check:

```bash
curl -s http://127.0.0.1:8700/health
curl -s -X POST http://127.0.0.1:8700/v1/systemone \
  -H 'Content-Type: application/json' \
  -d '{"model":"clm-latest","state":"my flask app returns 404",
       "questions":{"route":{"type":"choice","instructions":"Which team?",
         "criteria":{"coding":"bugs, code","research":"docs lookup"}}}}'
```

## Configuration (`runtime.yaml` → `plugins.clm`)

| key | default | meaning |
|---|---|---|
| `base_url` | `http://127.0.0.1:8700` | clm-serve address |
| `model` | `clm-latest` | head to serve decisions from |
| `timeout_s` | `5` | tool-call timeout |
| `route` | `false` | master switch for the routing hook |
| `route_threshold` | `0.6` | min. top probability to route a request |
| `route_timeout_s` | `2.0` | hook timeout (per-request path) |

Privacy: everything is local — no cloud backend exists for this plugin.

## Fine-tuning a JayNet head (later)

Only the 20M-param heads train, and they hot-reload. The planned head:
delegation-review verdicts mined from `trace.db` (accept / re-delegate /
reject on specialist reports) — a ~100 ms verifier replacing the expensive
delegation-review turn. See `docs/FINETUNING.md` in the CLM repo.
