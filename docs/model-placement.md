# Model placement (GPU / CPU slotting)

Where a model runs is data, not code. Two levels, both managed in
**Admin → Presets**:

- **Topology** — the *GPUs* editor lists the machine's cards: an id (the
  `ROCR_VISIBLE_DEVICES`/`CUDA_VISIBLE_DEVICES` value), a label and a VRAM
  figure per card. Any count works — one card, two, eight — and vendors/VRAM
  may be mixed. Removing a card that a preset still uses is refused.
- **Per preset** — each model preset has a device dropdown: one card
  (`1`), a subset (`0,2`), *All GPUs* (split across the whole topology), or
  *CPU* (no GPU). The value is just a comma-joined id list stored with the
  preset; `start-model.sh` turns it into the right `llama-server` flags
  (device export, `--split-mode layer` + `--tensor-split` weighted by the
  cards' VRAM, or `--n-gpu-layers 0` for CPU).
- **Binaries** — the *Binaries* editor names the available llama-server
  builds (`name → path + device_env`). Each preset picks one; empty means
  the launcher default (`LLAMA_BIN` env or the built-in path). This matters
  for mixed vendors: one process = one backend, so a preset pinned to a
  foreign vendor's card needs a matching binary — and splitting **one model**
  across mixed-vendor cards only works by pointing that preset at a **Vulkan**
  build (the only backend that sees all vendors in a single process).

What that buys you:

- **Two mid-size cards** (the default): brain on GPU 0, swappable specialist
  on GPU 1, embed + rerank on CPU.
- **One big card**: brain and specialist share it — set both presets to the
  same id.
- **Maximum brain size**: split one large model across every card
  (*All GPUs*) and run the specialist on CPU or leave it stopped.
- **Odd topologies**: 3+ cards, a big + a small card, CPU-only fallback —
  all just rows in the GPUs editor plus a dropdown choice per preset.

Placement follows the preset, so the model switcher keeps working: swapping
the specialist swaps *which* model is live, not where it runs. The `gpus` /
`gpu_info` / `binaries` blocks in `config/runtime.yaml` are only the factory
seed; after first boot the DB is the source of truth.

## Remote presets (another box on the LAN)

A preset with **remote** enabled + a `remote host` is a llama-server that
runs on *another machine* in your homelab. JayNet treats it exactly like a
local preset — it can fill a boot slot (brain/specialist), shows up in
`model.list`, and `model.use` returns its alias — with one difference:
**JayNet never launches, swaps, or stops it.** `model.use` and boot only
health-probe `http://<host>:<port>` and report *unreachable* if nothing
answers; `serve.start` and `start-model.sh` refuse remote presets outright.

Setup on the remote box:

- Run llama-server with `--host 0.0.0.0 --port <port>` (the preset's `port`
  is that listen port).
- Firewall it to your LAN — traffic is **plain HTTP**. "Local-first" here
  means "your homelab": requests leave the JayNet box, so the same trust
  considerations as any LAN service apply. No API key is required (the
  rendered proxy config sends `not-needed`, same as loopback).
- Remote presets occupy no local GPU/VRAM; device/binary/conf fields are
  ignored (hidden in the editor).

Because remote presets live in the preset catalog — not in *Cloud models* —
the cloud gate keeps classifying them as local, they carry no cost, and they
never need an API key. Use cloud models only for actual hosted providers.
