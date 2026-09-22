# Jev — decision-model plugin

[Open-Jev](https://github.com/Zefan-Cai/Open-Jev) is an open decision model
(LoRA + decision head on Qwen3.5-2B/9B, MIT code / Apache-2.0 adapters):
state + typed questions in, **calibrated probabilities** out — one forward
pass, no text generation, nothing to parse.

This plugin wires it into JayNet two ways:

- **`jev.decide` tool** — the brain can ask choice / yes-no / score
  questions and get probabilities (routing, triage, classification with a
  real confidence number).
- **Delegation routing** (`route_request` hook) — every incoming request is
  classified into a strength tag from your `models.strengths` registry.
  Clearing the threshold (`route_threshold`, default 0.6) routes the run to
  the matching specialist; anything else falls through to the keyword
  router. Off by default (`plugins.jev.route: true` to enable).
  **Measured 2026-09-22: mixed.** Open-Jev 2B (local) is not good enough —
  trained on synthetic business decisions, it classified coding requests
  confidently as "general"; keywords won. Hosted TypeSafe Jev
  (`backend: openrouter`) routed the same 20 real prompts nearly perfectly
  (coding/research 0.92–1.00, vision 0.99, chat → general 0.96, ~0.4s) —
  the idea holds, the open weights aren't there yet. Ships with
  `route: false`: cloud-routing every request's text is the wrong default
  for a local-first box.
  **Update 2026-09-22, option C: [jevify](https://github.com/fidecastro/jevify).**
  Skip the trained checkpoint entirely — jevify makes a model you ALREADY
  serve (e.g. the coding specialist) answer the same typed questions from
  its next-token logprobs, and serves the identical Jev API this plugin
  speaks. Fully local, no extra weights, ~100 ms per question warm. Their
  frozen suites put a 27B-class llama.cpp endpoint at 46/52 on hard typed
  policy questions (embedding/reranker kinds: ~half that) — good enough for
  an advisory routing hint, which is exactly how the hook uses it. Setup
  below.

## Setup: jevify on the specialist (recommended local backend)

No new model, no new GPU burden — jevify borrows the specialist server you
already run (any llama.cpp/vLLM endpoint with logprobs works):

```bash
uv tool install jevify
cp plugins/jev/jevify-recipe.example.yaml specialist.llamacpp.yaml
# edit model.name and endpoint.base_url to the specialist's alias/port
jevify probe specialist.llamacpp.yaml     # verifies readout rungs, fills
                                          # the answer-token ids
jevify serve specialist.llamacpp.yaml --port 8600
```

Then point the plugin at it — same System One contract, just a different
`base_url` and `model`:

```yaml
plugins:
  jev:
    enabled: true
    base_url: http://127.0.0.1:8600
    model: qwen3-8-27b-turbo   # the recipe's model.name
    route: true                # advisory hook; keyword router stays fallback
```

Keep it running with a systemd unit like the other sidecars
(`ExecStart=jevify serve …`, after the specialist's llama-server). Caveats:
each run start pays a state ingest on the specialist (llama.cpp prefix
caching reuses the static head), and 46/52 accuracy earns a *hint*, not a
veto — the hook is advisory, keywords remain the fallback, and
`brain_mode: dispatch` still owns the hard "no inline coding" gate.

## Setup: the Open-Jev sidecar server (alternative)

The plugin is pure HTTP — the model server runs separately (torch + the
pinned Qwen base weights are heavy; keep them out of JayNet's venv):

```bash
git clone https://github.com/Zefan-Cai/Open-Jev.git
cd Open-Jev
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[train]'
hf download ZefanCai/Open-Jev-2B \
  --revision 0c7aa498b1627be8da4acf34c863ff0ee0a92785 \
  --local-dir models/Open-Jev-2B
# GPU (2B BF16 ≈ 5 GB VRAM):
python -m jev.server --checkpoint models/Open-Jev-2B/package/checkpoint \
  --device cuda:0 --max-length 4096 --batch-size 1 --no-prefix-cache
# CPU works for experiments (slower): --device cpu
```

The server listens on `http://127.0.0.1:8791`. Check it:

```bash
curl -s http://127.0.0.1:8791/v1/systemone -H 'Content-Type: application/json' -d '{
  "model": "open-jev",
  "state": "The build fails with a segfault after the last commit.",
  "questions": {"route": {"type": "choice",
    "instructions": "Which team handles this?",
    "criteria": {"engineering": "Software defects", "billing": "Refunds"}}}}'
```

## Config (runtime.yaml → plugins.jev, or admin → Config)

| key | default | what |
| --- | --- | --- |
| `backend` | `""` (local) | `openrouter` = TypeSafe's hosted Jev via OpenRouter's alpha Decisions API |
| `base_url` | `http://127.0.0.1:8791` | local backend: where the Open-Jev server listens (`8600` for a jevify sidecar) |
| `endpoint` | OpenRouter decisions URL | openrouter backend override |
| `model` | `open-jev` / `~typesafe/jev-latest` | model id per backend |
| `api_key_env` | `OPENROUTER_API_KEY` | env var with the OpenRouter key |
| `timeout_s` | `5` | tool-call timeout |
| `route` | `false` | route_request hook on/off (ships off — the measured verdict was "stay keyword") |
| `route_threshold` | `0.6` | min top probability to route on |
| `route_timeout_s` | `2.0` | hook timeout (runs per request; ~0.5s warm on a 2B GPU) |
| `allow_cloud_route` | `false` | privacy opt-in: without it the hook REFUSES the openrouter backend |

**Privacy:** the local backend keeps everything on the box. With
`backend: openrouter` the judged text leaves the machine — and since the
routing hook fires at run START (before the run's taint/approval machinery
exists), the hook refuses the cloud backend entirely unless
`allow_cloud_route: true` is set. The `jev.decide` tool is an explicit
per-call action and is not gated this way.

## OpenRouter backend (hosted Jev)

No sidecar needed — TypeSafe's Jev via OpenRouter speaks the identical
System One contract (`POST /api/alpha/decisions`):

```yaml
plugins:
  jev:
    backend: openrouter        # model defaults to ~typesafe/jev-latest
```

Needs `OPENROUTER_API_KEY` in the env file. Billed per input token
(fractions of a cent per call); decisions include a `confidence` field the
local checkpoint also emits.

Then enable the plugin in admin → Plugins and restart. If the server is
down, `jev.decide` returns a clear error and routing silently falls back to
keywords — JayNet keeps working without it.
