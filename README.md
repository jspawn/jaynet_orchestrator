# Orchestrator

Local-first LLM orchestrator for a local workstation — any GPU count (mixed
vendors/VRAM welcome) or CPU-only. A FastAPI web console
drives an agent loop over local llama-server models; cloud models are
escalation only (local first — cloud when local can't solve it). Multi-user
(login + TOTP 2FA), per-user chats and projects, ~100 tools, on-demand skills,
voice channel, scheduled runs, and an admin console for prompts, budgets,
tools, users, flags and the model catalog.

## What's special

- **Multi local models, one brain.** The orchestrator model reasons,
  plans and routes; a *specialist* slot holds a swappable second model
  (coding, research, security, …) that heavy sub-tasks are delegated to via
  `code.delegate` / `agent.spawn`. CPU-only embed + rerank servers back the
  RAG tools. All of them run side by side behind one LiteLLM proxy.
  Device placement is per preset, not hard-wired — see
  [Model placement](#model-placement-gpu--cpu-slotting).
- **The model switcher.** A curated preset catalog describes every servable
  model (weights, port, GPU, VRAM, strengths). `model.use('<name>', swap: true)`
  stops the current specialist model and boots another one in place — the brain
  does this itself mid-chat when a task calls for a different specialist.
  Static ports + LiteLLM aliases mean no proxy re-registration; `served_id`
  mismatch checks catch a wrong model on a slot.
- **Strengths-aware routing.** Each preset carries capability tags
  (`strengths: [coding, research, …]`) that are injected into the brain's
  system prompt, so it knows *which* specialist is live and won't send a
  coding task to a research model.
- **Admin-managed catalog.** Presets live in a SQLite DB
  (`/srv/data/presets.db`, seeded once from `runtime.yaml`) and are edited in
  the admin UI — add models, retune launch flags, reassign slots, change
  device placement, no restarts of the web service. Cloud models too
  (admin → Presets → Cloud models): aliases, provider ids, costs, thinking
  defaults and fallbacks are DB rows; saving re-renders the LiteLLM proxy
  config to `/srv/data/litellm.yaml` and reloads the proxy. API keys never
  enter the DB — a row stores only the env var *name*, keys stay in
  `orchestrator.env`.
- **Local-first with guardrails.** Cloud calls (llm.call) are approval-gated
  and privacy-aware (private tool results never leave the box without
  consent); budgets cap iterations/tokens/cost per run; every step is traced.

## Model placement (GPU / CPU slotting)

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

## Preparing llama.cpp

Reference build script: `/srv/llama/build_tools.sh` (host-side, not in this
repo). It clones upstream `ggml-org/llama.cpp` into one tree per backend and
builds `llama-server` / `llama-cli` / `llama-bench`:

```
./build_tools.sh llama            # both backends
./build_tools.sh llama rocm       # one backend only
./build_tools.sh --clean llama    # rebuild from scratch
```

- **ROCm/HIP tree** (`/srv/llama/llama.cpp-rocm`): `GGML_HIP=ON`,
  `AMDGPU_TARGETS=gfx1201` (R9700/RDNA4 — set to *your* arch), rocWMMA
  flash-attn auto-enabled when the headers exist (`pacman -S rocwmma`), ROCm's
  clang as compiler, `-march` tuned to the host CPU. Needs ROCm ≥ 6.4.1 for
  RDNA4 and the user in `video` + `render` groups.
- **Vulkan tree** (`/srv/llama/llama.cpp-vulkan`): `GGML_VULKAN=ON`,
  vendor-neutral — needs `vulkan-headers`, `spirv-headers`, `shaderc` and the
  vendor's ICD (`vulkan-radeon`, NVIDIA driver, …).
- Both build **headless** (`LLAMA_BUILD_UI=OFF` + `LLAMA_BUILD_WEBUI=OFF`):
  the servers run behind LiteLLM, and skipping the embedded web UI also skips
  its fragile npm/asset build step.

Sanity check after building: `build/bin/llama-server --list-devices`.

**Multi-GPU builds.** One `llama-server` process uses exactly one backend, so
pick per what the cards are:

- **Same vendor, different generations** — one HIP build listing all archs,
  e.g. `-DAMDGPU_TARGETS="gfx1201;gfx1100"`. Fatter binary, but one server
  sees every AMD card.
- **NVIDIA** — `GGML_CUDA=ON` with `CMAKE_CUDA_ARCHITECTURES` for your cards.
- **Mixed vendors** — a HIP build can't touch an NVIDIA card and vice versa.
  To *split one model* across mixed cards, build **Vulkan**: it's the only
  backend that covers all vendors in a single process. Register both builds
  under **Admin → Presets → Binaries** (e.g. `rocm` for the single-vendor
  presets, `vulkan` for the cross-vendor split) and pick per preset.
- **CPU-only** — no backend flags at all.

Whatever you build, the GPU ids you enter in **Admin → Presets → GPUs** must
be the ids the binary actually exposes — for HIP/CUDA that's the
`ROCR`/`CUDA` device index; for Vulkan check `--list-devices`, the numbering
can differ. The `device_env` on the binary entry is what `start-model.sh`
exports to pin cards (`HIP_VISIBLE_DEVICES` / `CUDA_VISIBLE_DEVICES` /
`GGML_VK_VISIBLE_DEVICES`).

## Setup

Assumptions: Linux, systemd `--user` services, AMD GPUs via ROCm (NVIDIA/CPU
works too — adjust the presets and the llama.cpp build). Install root is
`/srv/orchestrator`, data lives in `/srv/data`.

1. **llama.cpp.** Build `llama-server` for your hardware — see
   [Preparing llama.cpp](#preparing-llamacpp) (multi-GPU notes included).
   The presets expect the binary at
   `/srv/llama/llama.cpp-rocm/build/bin/llama-server` (override with
   `LLAMA_BIN`). `tools.serve` sources `/srv/llama/rdna4-env.sh` before
   launches (ROCm env). The service user must be in the `video` + `render`
   groups.
2. **Models.** Download GGUFs under `/srv/models/…` and point the presets at
   them. The shipped catalog: brain = Hermes3.6-35B-A3B (GPU 0), specialist =
   Fable-27B (GPU 1, swappable: tess/ornith/agents1/dolphin), embed + rerank
   (CPU). Adjust `presets/*.conf` to your hardware (ctx size, KV quant, VRAM)
   and the device placement in admin → Presets — e.g. one big brain split
   across all GPUs with the specialist on CPU or stopped.
3. **Python envs.**
   ```
   python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-web.txt
   python -m venv litellmenv && litellmenv/bin/pip install -r requirements-litellm.txt
   .venv/bin/pip install -r requirements-test.txt   # dev only
   ```
4. **Env file (secrets + paths).**
   ```
   install -Dm600 systemd/orchestrator.env ~/.config/orchestrator.env
   # edit: ORCH_SESSION_SECRET, ORCH_WEB_TOKEN, LITELLM_MASTER_KEY,
   # first-boot ORCH_ADMIN_USER/PASSWORD, cloud keys (optional)
   ```
5. **systemd user units.**
   ```
   mkdir -p ~/.config/systemd/user
   cp systemd/*.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now litellm-proxy orchestrator-web
   loginctl enable-linger "$USER"   # keep services up without a login session
   ```
   The web service's process manager boots the models itself (`processes:` in
   `runtime.yaml`). The `llama-*.service` units are headless fallbacks — keep
   them disabled while orchestrator-web runs (port fight).
6. **First run.** Browse to `http://<host>:8071`, log in with the seeded
   admin, then remove `ORCH_ADMIN_*` from the env file. The preset catalog
   self-seeds into `/srv/data/presets.db`; manage it in **Admin → Presets**.
   Check **Admin → Status** for service health.

Optional pieces: SearXNG container for `web.search` (`tools.web.search_endpoint`),
system Chromium or a Playwright CDP container for `browser.*`, `firejail` for
the code sandbox, cloud API keys for `llm.call` escalation.

## Architecture

```
browser / voice client
        │  HTTP + SSE
  web/server.py ──────────────┐
        │                     │ admin page, flags, watchdog reports
  runtime/loop.py (agent loop: model ↔ tools, budgets, compaction, trace)
        │
  tools/ (auto-discovered)  skills/ (prompt playbooks, loaded on demand)
        │
  LiteLLM proxy (:4000) ── local llama-servers (brain :8090, specialist :8080,
        │                  embed :8095, rerank :8096 — launched by the
        │                  process manager from the preset catalog)
        └─ cloud: Kimi, Qwen, Gemini, GLM (via llm.call, approval-gated)
```

A run: `POST /api/chat` → slash commands routed (`/goal`, `/compact`, `/imp`,
`/wgs`) or the agent loop starts. Tool selection ships ~16 core tools and adds
namespaces by keyword (`tool_selection` in runtime.yaml); the set is frozen
per run. State-changing and cloud calls pause for human approval. Every step
is traced to `trace.db` and streamed to the UI over SSE.

## Layout

| Path | What |
|---|---|
| `web/` | FastAPI server, auth, chats/users/goals/projects stores, watchdog, static UI |
| `runtime/` | agent loop, tool registry/selector, budgets, compaction, scheduler, preset store |
| `tools/` | tool implementations, one namespace per dir (fs, code, git, web, rag, llm, …) |
| `skills/` | SKILL.md playbooks the model loads via `skill.load` |
| `prompts/` | `orchestrator-gate.md` — the live system prompt (~850 tok) |
| `config/` | `runtime.yaml` (main config), `litellm.yaml` (proxy config SEED — rendered to `/srv/data/litellm.yaml`), `quick-replies.yaml`, chat templates |
| `presets/` | factory llama-server presets (seed the DB catalog; edit via admin UI afterwards) |
| `scripts/` | `orch` CLI, `start-model.sh` (preset launcher), dev benchmarks |
| `systemd/` | user units + `orchestrator.env` template |
| `docs/` | testing-harness guide |
| `tests/` | pytest suite (~630 tests, no network) |

## Configuration

- **`config/runtime.yaml`** — everything: system prompt, budgets, tool
  selection, privacy/confirmation gates, GPU posture, process manager, goal
  mode, watchdog, voice, per-tool settings. Admin page edits persist as DB
  overrides applied live.
- **Preset catalog** — `/srv/data/presets.db` (admin → Presets). The
  `models.presets` block in runtime.yaml is the factory seed only.
- **`config/litellm.yaml`** — factory SEED for the proxy config. At runtime
  the proxy reads `/srv/data/litellm.yaml`, rendered from the DB (preset
  catalog + cloud models) at boot (ExecStartPre) and on every admin save.
- **Secrets** — `~/.config/orchestrator.env` on the host (template:
  `systemd/orchestrator.env`). Never commit it.
- **Data** — lives outside the repo (`/srv/data`): chats.db, users.db,
  trace.db, presets.db, rag.db, uploads, outputs, projects.

## Notable subsystems

- **Goal mode** (`/goal`) — user-bound objective pursued one turn per run by
  the supervisor in `web/goals.py`, with ceilings and a completion judge.
- **Watchdog** (`web/watchdog.py`) — postmortem on stuck/failed runs; writes a
  capped, deduped report visible in the admin Flags tab.
- **Process manager** (`runtime/process_manager.py`) — launches/stops
  llama-servers from the preset catalog inside the web unit's cgroup; replaces
  systemd units for models.
- **RAG** (`tools/rag/`) — embed (:8095) + optional rerank (:8096), direct
  HTTP, not via LiteLLM.
- **Scheduler** (`runtime/scheduler.py`) — `schedule.*` tools + web tick fire
  recurring/one-shot runs.

## Development

Dev checkout: `/srv/orch-dev`. Live install: `/srv/orchestrator` — never edit
live directly; deploy = git pull + `systemctl --user restart`.

Run the suite from the dev checkout with the live venv:

```
cd /srv/orch-dev && /srv/orchestrator/.venv/bin/python -m pytest tests/ -q
```

Conventions: no cross-test imports (copy helpers), monkeypatch instead of
network, comments short and plain. See `docs/testing-harness.md` for the
`test.run` harness and `ToDos_for_later.md` for parked ideas.
