# Model placement (GPU / CPU slotting)

Where a model runs is data, not code. Two levels, both managed in
**Admin → Presets**:

- **Topology** — the *GPUs* editor lists the machine's cards: an id (the
  `ROCR_VISIBLE_DEVICES`/`CUDA_VISIBLE_DEVICES` value), a label and a VRAM
  figure per card. Any count works — one card, two, eight — and vendors/VRAM
  may be mixed. Removing a card that a preset still uses is refused.
- **Per preset** — each model preset has a device picker (one checkbox per
  card, plus CPU): one card (`1`), any subset (`0,2`), every card (`0,1`), or
  *CPU* (no GPU). The value is just a comma-joined id list stored with the
  preset; `start-model.sh` turns it into the right `llama-server` flags
  (device export, `--split-mode layer` + `--tensor-split` weighted by the
  cards' VRAM, or `--n-gpu-layers 0` for CPU). Each GPU row also shows the
  card's *live* free VRAM (rocm-smi, or nvidia-smi on CUDA boxes), so you
  can see what fits before you save.
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

## Hardware-wide swaps + swap-back

`model.use(preset, swap: true)` frees whatever the incoming preset needs —
its **port** and **every pinned GPU**, not just the slot that shares its
port. A brain split across both cards is the real occupant of GPU 1 even
though it answers on the brain port; the eviction planner stops the whole
set (serve-managed servers and boot-posture slots via the process manager —
auto-restart stays disarmed; systemd units and remote boxes are never
touched), waits for the VRAM to actually release on every affected card,
and only then loads the incoming model. So the extremes both work: one big
model claiming the whole machine, and the everyday brain/specialist split.

When `code.delegate` triggers such a swap (a strength-tagged specialist
needs hardware the brain sits on), it passes `include_brain` and — after
the child run finishes — **restores what it evicted**, brain first, waiting
until each model answers again. That is the multi-GPU swap lifecycle: brain
on all cards → delegate evicts it → specialist works → brain reloads on its
own port, aliases just work again. Restore failures are reported in the
delegate result and visible on the Processes tab, never silent. Opt out
with `models.swap_back: false` in `config/runtime.yaml`. Direct
`model.use(..., swap: true)` calls never evict the brain (that would kill
the current run's model mid-turn) — only delegate's restore-covered path
may.

## Boot slots: empty allowed, extra specialists

**Admin → Presets → Boot model slots** maps each managed process to the
preset it boots. Every slot except **brain** can be set to **(none)** — the
process then stays down (shown as *disabled (slot empty)* on the Processes
tab, and `start-model.sh <slot>` explains why). This is how you run without
a specialist or without the RAG servers. An empty **specialist** keeps its
LiteLLM alias alive by pointing it at the brain (same as the down-server
fallback), so `agent.spawn(model="local-specialist")` keeps working.

**specialist2 / specialist3** are optional extra specialist slots, empty by
default. Assign a preset and (re)start the process to bring up a second or
third specialist; they render as the LiteLLM aliases
`local-specialist2` / `local-specialist3` only while assigned.

**vision / stt** are the optional CPU helper slots, also empty by default:
a small multimodal llama-server (`local-vision`, backs
`llm.call images=[...]`) and a whisper.cpp speech-to-text server (backs
`audio.transcribe` and the chat mic button). Details and example models:
[models.md](models.md#vision-stt-helpers).

## Remote presets (another box on the LAN)

A preset with **remote** enabled adopts an **already-running
OpenAI-compatible server** on *another machine* in your homelab — a
llama-server, vLLM, Ollama, anything speaking `/v1`. JayNet treats it like a
local preset — it can fill a boot slot (brain/specialist), shows up in
`model.list`, and `model.use` returns its alias — with one difference:
**JayNet never launches, swaps, or stops it.** The process manager skips a
remote slot at boot (*remote — probe only* on the Processes tab) and refuses
manual starts; `model.use` health-probes the endpoint and reports
*unreachable* if nothing answers, or *authentication required* if the server
answers 401/403 (set the preset's `api_key_env` for keyed servers); `serve.start` and `start-model.sh` refuse remote presets
outright. Adoption details, backends and capabilities:
[models.md → Adopting a server that's already running](models.md#adopt-existing-server).

Setup on the remote box:

- The **endpoint** is either a bare host (`192.168.1.50` — the preset's
  `port` field is the listen port) or a full URL
  (`http://vllm-box:8000`, `http://ollama-box:11434` — a URL's own port
  wins; portless URLs get the scheme default). The server must answer
  `/v1/models`; on multi-model servers (Ollama, vLLM) the preset's
  `served_id` picks its model out of the list.
- **Backend + capabilities**: the backend label (`llama` default, `vllm`,
  `ollama`, `openai`) decides which llama-only extras (jinja thinking
  switch, llamacpp metrics) apply; `caps.thinking` / `caps.vision` opt in
  or out explicitly.
- Firewall it to your LAN — traffic is **plain HTTP** unless you put TLS in
  front. "Local-first" here means "your homelab": requests leave the JayNet
  box, so the same trust considerations as any LAN service apply. The
  endpoint must be **keyless** (the rendered proxy config sends
  `not-needed`, same as loopback; per-preset keys are parked for the
  managed-backend layer).
- Remote presets occupy no local GPU/VRAM; device/binary/conf fields are
  ignored (hidden in the editor).

Because remote presets live in the preset catalog — not in *Cloud models* —
the cloud gate keeps classifying them as local, they carry no cost, and they
never need an API key. Use cloud models only for actual hosted providers.
