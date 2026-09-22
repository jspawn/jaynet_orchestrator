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
  router. Disable with `plugins.jev.route: false`.

## Setup: the sidecar server

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
| `base_url` | `http://127.0.0.1:8791` | where the Open-Jev server listens |
| `timeout_s` | `5` | tool-call timeout |
| `route` | `true` | route_request hook on/off |
| `route_threshold` | `0.6` | min top probability to route on |
| `route_timeout_s` | `2.0` | hook timeout (runs per request; ~0.5s warm on a 2B GPU) |

Then enable the plugin in admin → Plugins and restart. If the server is
down, `jev.decide` returns a clear error and routing silently falls back to
keywords — JayNet keeps working without it.
