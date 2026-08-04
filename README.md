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
- **The Studio.** Admins build new skills, chains, API connectors and Python
  tools right in the admin UI — guided by the built-in
  `writing-great-skills` playbook or drafted by the local model. Everything
  lands in a custom layer under `$ORCH_DATA/custom/`, stacked over the repo
  built-ins (custom wins on name clash, survives git pulls), and
  exports/imports as `.jaypack` archives to share between installs.

## Where JayNet fits — and for whom

The self-hosted agent landscape splits into three camps, and JayNet deliberately
isn't any of them:

- **Chat frontends** (Open WebUI, LibreChat, Anything-LLM) give you multi-user
  chat + RAG over local models, but treat models as fixed endpoints and agents
  as a plugin afterthought. JayNet inverts that: the agent loop is the product,
  and the models are *managed infrastructure* the agent itself can reconfigure
  (swap specialists, place them on GPUs) mid-chat.
- **Agent platforms** (Dify, Flowise, n8n, AGiXT) give you visual workflows,
  plugin catalogs and team features — at the cost of containers, complexity,
  and no idea what hardware you have. JayNet's answer to reusable pipelines is
  **chains** (small YAML files the model runs via `chain.run`) and to the
  plugin ecosystem an **MCP bridge** (`mcp.list`/`mcp.call`) — both configured
  as text, both running inside the same single Python service.
- **Agent frameworks** (LangGraph, CrewAI, smolagents) are libraries for
  building what JayNet already is. If you want to write Python to get an
  agent, use those; JayNet is the finished thing you point at your GPUs.

So JayNet is for the **single-operator or small team with a GPU workstation**
who wants a private, multi-model agent that owns its whole stack — models,
memory, tools, scheduling, verification — without a container orchestra, and
who values knowing exactly what ran (trace.db, coroner reports) over having a
marketplace of integrations. If you need SSO, teams of fifty, visual flow
designers, or multi-node serving, the camps above serve you better.

Two ideas worth knowing were adapted from the neighbours: AGiXT-style reusable
pipelines became chains (`chains/*.yaml`: sequential `agent` + local `prompt`
steps with `{{placeholders}}`), and the MCP ecosystem is reachable through the
`mcp.*` tools instead of native integrations — see `tools.mcp.servers` in
`config/runtime.yaml`.

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

## Minimum hardware requirements

| Tier | What you need | What you get |
|---|---|---|
| **Minimal** (quick start) | x86_64 Linux, 8 GB RAM, 10 GB disk, no GPU | Full agent chat with the default brain (Qwen3-4B Q4, ~4 GB RAM incl. context), CPU inference |
| **Full setup** | 16 GB RAM, 100 GB disk, GPU sized to your brain: 8 GB VRAM (4–8B) · 16 GB (14B) · 24–32 GB (30B-class MoE) | GPU-served brain, embed + rerank on CPU (~2 GB RAM), RAG, model switcher |
| **Production** (example) | 64 GB RAM, 2× 32 GB GPU | 35B-class brain + 27B specialist side by side — see [Example setup](#example-setup-wolf) |

Multi-GPU and mixed-vendor are first-class (per-preset device placement) —
nothing needs to match a reference machine. CPU-only works everywhere; it's
just slower.

## Quick start (minimal, ~15 min)

One CPU, one small model, no GPU build, no proxy, no systemd — enough to
chat and evaluate JayNet before committing to the full setup below.

**Automated (Linux x86_64):**

```bash
git clone <repo> /srv/orchestrator && cd /srv/orchestrator
scripts/quickstart.sh          # fetches llama-server + a model, sets up everything
```

Then run the two commands it prints (model server + app). **Manual** —
the same steps by hand:

```bash
# 1. base packages: git, python3.10+, uv (https://docs.astral.sh/uv/)

# 2. llama-server: grab a prebuilt CPU binary from the llama.cpp releases
#    page (linux-x64), no compilation needed:
#    https://github.com/ggml-org/llama.cpp/releases

# 3. one small GGUF, e.g. Qwen3-4B Q4_K_M (~2.5 GB) into ./models/ —
#    license-clean picks: docs/models.md (after step 4 you can use
#    scripts/pull-model Qwen/Qwen3-4B-GGUF for this)

# 4. the code + Python env
git clone <repo> /srv/orchestrator && cd /srv/orchestrator
uv venv .venv && uv pip install --python .venv/bin/python \
    -r requirements.txt -r requirements-web.txt

# 5. data dir (or edit the four /srv/data keys in config/runtime.yaml)
sudo mkdir -p /srv/data && sudo chown "$USER" /srv/data
```

One config edit: in `config/runtime.yaml`, **comment out the `processes:`
section** (it auto-launches the example host's model presets, which don't
exist on your machine). Then two terminals:

```bash
# terminal 1 — the model (port 4000 is where the app looks by default)
./llama-server -m models/<your-model>.gguf --port 4000 -c 16384

# terminal 2 — the app (admin password is generated and logged on first boot)
.venv/bin/uvicorn web.server:app --host 127.0.0.1 --port 8071
```

Open `http://127.0.0.1:8071`, log in with the generated admin password from
the terminal-2 log, and chat. The app talks to llama-server's OpenAI-compatible
endpoint directly — no LiteLLM proxy needed for a single model.

Not in minimal mode (needs the full setup): specialist/cloud models, RAG
embeddings, the model switcher, systemd autostart. When convinced, continue
with [Preparing llama.cpp](#preparing-llamacpp) and the full Setup — the data
dir carries over.

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
`/srv/orchestrator`, data lives in `/srv/data` — both are just defaults,
overridable via `ORCH_HOME` / `ORCH_DATA` in the env file (see
`runtime/paths.py`, the single source of truth for paths).

Everything under `config/`, `presets/`, `systemd/` and `example_configs/` is
a **working example deployment** — adapt it to your host. Secrets never live
in the repo: config files reference env var *names*, values only enter via
the env file (step 4).

**Automated:** `scripts/setup.sh` covers steps 0 + 3–6 (prereq check, venvs,
env file with generated secrets, systemd units, linger; `--start` to launch
services, `--with-tools` for the optional extras). Steps 1–2 (llama.cpp,
models) stay manual — or use `scripts/pull-model` for the downloads. After
anything install-related, `scripts/orch --doctor` validates the whole setup.
The manual path:

0. **Base packages.** `git`, Python 3.10+ (developed on 3.13) and
   [`uv`](https://docs.astral.sh/uv/) — the Python envs below are uv-managed
   (`pip` works too, just slower). Optional at runtime, install when you
   want the feature: `firejail` (code sandbox), system Chromium or a
   Playwright CDP container (`browser.*`), a SearXNG container
   (`web.search`), ROCm/CUDA drivers for GPU inference (step 1).
1. **llama.cpp.** Build `llama-server` for your hardware — see
   [Preparing llama.cpp](#preparing-llamacpp) (multi-GPU notes included).
   The presets expect the binary at
   `/srv/llama/llama.cpp-rocm/build/bin/llama-server` (override with
   `LLAMA_BIN`). `tools.serve` sources `/srv/llama/rdna4-env.sh` before
   launches (ROCm env). The service user must be in the `video` + `render`
   groups.
2. **Models.** Download GGUFs under `/srv/models/…` (`scripts/pull-model
   <hf-repo>` does this interactively; license-clean picks:
   [docs/models.md](docs/models.md)) and point the presets at
   them. The shipped default catalog: brain = Qwen3-4B (GPU 0 or CPU),
   embed + rerank = Qwen3 0.6B (CPU) — a real multi-GPU lineup is in
   [Example setup](#example-setup-wolf). Adjust `presets/*.conf` to your
   hardware (ctx size, KV quant, VRAM) and the device placement in
   admin → Presets — e.g. one big brain split across all GPUs with the
   specialist on CPU or stopped.
3. **Python envs.**
   ```
   uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt -r requirements-web.txt
   uv pip install --python .venv/bin/python -r requirements-tools.txt      # recommended: charts + browser rendering
   uv venv litellmenv && uv pip install --python litellmenv/bin/python -r requirements-litellm.txt
   uv pip install --python .venv/bin/python -r requirements-test.txt       # dev only
   ```
   `requirements.txt` is the minimal core; `requirements-tools.txt` adds the
   heavy optional extras (matplotlib, playwright). Everything in it is loaded
   lazily — the core runs fine without it. LiteLLM gets its own venv because
   its `[proxy]` extra pins exact versions of httpx/pydantic/tiktoken that
   would otherwise fight the runtime venv.
4. **Env file (secrets + paths).**
   ```
   install -Dm600 example_configs/orchestrator.env.example ~/.config/orchestrator.env
   # edit: ORCH_SESSION_SECRET, ORCH_WEB_TOKEN, first-boot
   # ORCH_ADMIN_USER/PASSWORD, cloud keys (optional), LITELLM_MASTER_KEY
   # (optional — the proxy binds 127.0.0.1, so localhost-only installs can
   # skip it),
   # ports if 4000/8071 are taken (ORCH_LITELLM_PORT/ORCH_WEB_PORT — for the
   # proxy also edit orchestrator.litellm_base in runtime.yaml)
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
   `runtime.yaml`) — there are no separate model units anymore.

   ⚠ **Don't skip the `loginctl enable-linger` line.** Without it systemd
   kills your `--user` services at logout — everything looks fine until you
   close the SSH session and the whole stack (proxy, web console, models)
   goes down with it. Check with `loginctl show-user "$USER" | grep Linger`
   (want `Linger=yes`).
6. **First run.** Browse to `http://<host>:8071`, log in with the seeded
   admin, then remove `ORCH_ADMIN_*` from the env file. The preset catalog
   self-seeds into `/srv/data/presets.db`; manage it in **Admin → Presets**.
   Check **Admin → Status** for service health — or run
   `scripts/orch --doctor` for a full install validation (env file, paths,
   ports, proxy, DBs, GPU, linger).

   Post-install checklist: change the admin password (Account → Security) ·
   set your location + timezone (Account → Settings — used for local queries
   and freshness) · create a per-user API token if you'll use the CLI/API ·
   tune quick settings (nerd mode, theme, run preferences) to taste.

Optional pieces: see step 0 — plus cloud API keys in the env file for
`llm.call` escalation.

## Example setup (wolf)

The deployment this repo's shipped config mirrors — a single workstation
running everything:

- **Hardware:** AMD Ryzen 9 7950X (16C/32T), 64 GB RAM,
  2× AMD Radeon AI PRO R9700 32 GB (RDNA4, ROCm), 2× 1 TB NVMe
  (`/srv/models` and `/srv/data` on separate disks)
- **Models:** brain = Qwen3.6-35B-A3B MoE on GPU 0 (`:8090`); specialist =
  Fable-27B on GPU 1 (`:8080`), swappable mid-chat via `model.use` (tess /
  ornith / agents1 / dolphin presets); embed = Qwen3-Embedding-8B and
  rerank = Qwen3-Reranker-0.6B, both CPU-only (`:8095`/`:8096`)
- **Stack:** llama.cpp self-built (ROCm gfx1201 + Vulkan trees), LiteLLM
  proxy on `:4000`, web console on `:8071` — all systemd `--user` services
  with the process manager supervising the model servers
- **Around it:** nginx + Let's Encrypt on a separate host (`ask.jaynet.ch`),
  a SearXNG container for `web.search`, cloud models (kimi, glm, gemini,
  qwen) as approval-gated escalation only

Yours will differ — that's the point of the preset catalog. The defaults a
fresh install starts from are in [docs/models.md](docs/models.md).

## Reverse proxy (optional, for remote access)

The console listens on `:8071` plain HTTP; put nginx in front for TLS and
remote access. A complete, annotated example lives at
[`example_configs/nginx.conf.example`](example_configs/nginx.conf.example). The four things that
matter:

1. **SSE must not be buffered** (`proxy_buffering off` on `/api/stream/`),
   or live tokens arrive in clumps; long read timeouts for long runs.
2. **Forward the headers** (`Host`, `X-Forwarded-For/Proto`) — and set the
   trusted proxy IP via `ORCH_FORWARDED_ALLOW_IPS` in the env file (the web
   unit passes it to uvicorn's `--proxy-headers --forwarded-allow-ips`).
3. **Block dotfiles at the edge** (`/.env`, `/.git`, … never reach the app).
4. Optional hardening in the example: LAN/VPN-only `allow/deny`, basic-auth
   in front of the app login, rate limiting, HSTS + security headers.

## Uninstall

Everything JayNet touches is four places: the systemd units, the env file,
the install root, the data dir. In order:

```bash
# 1. services (stopping orchestrator-web also SIGKILLs its llama-server
#    children via KillMode=mixed — no stragglers)
systemctl --user disable --now orchestrator-web litellm-proxy
rm ~/.config/systemd/user/{orchestrator-web,litellm-proxy}.service
systemctl --user daemon-reload

# 2. env file (paths + secrets)
rm ~/.config/orchestrator.env

# 3. install root (code, config, venvs)
rm -rf /srv/orchestrator

# 4. data — DESTRUCTIVE: chats, users, projects, wiki, memory, uploads,
#    Studio custom layer. Back up first if unsure (docs/upgrading.md).
rm -rf /srv/data
```

Leftovers to remove by hand if you set them up: the nginx vhost + Let's
Encrypt certs (`example_configs/nginx.conf.example`), `loginctl disable-linger "$USER"`,
and the host-side pieces outside this repo — llama.cpp builds, GGUF models,
a SearXNG container.

## HTTP API for native clients

The stable contract — endpoints, shapes, auth, change policy — lives in
[`docs/api.md`](docs/api.md). In short: native/CLI clients authenticate with
a **per-user API token** created in Account → Security → API tokens, sent as
`Authorization: Bearer jn_…`. The token acts as that user (budgets, tool
toggles, projects all apply) and is individually revocable. Key endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /api/chat` | start a run: `{"message": "…"}` → `{"run_id"}` |
| `GET /api/stream/{run_id}` | SSE feed: tool calls, tokens, final answer |
| `POST /api/approve/{run_id}` · `/api/answer/{run_id}` · `/api/cancel/{run_id}` | confirmations, ask.user answers, stop |
| `POST /api/voice` | native-client turn with server-managed conversation: `{"text": "…"}`; pass the returned `conversation_id` to continue. Default `voice: true` = short spoken-style answers; chat clients send `voice: false` for full markdown with thinking and normal budgets; `stream: true` returns a `run_id` for the SSE feed |
| `GET /api/health` · `/api/tools` | liveness + version (no auth), tool catalog |

`ORCH_WEB_TOKEN` (env file) is a separate **global admin** bearer for server
automation — unscoped and non-expiring, so keep it out of client apps; rotate
it via env change + restart if it may have leaked.

## CLI (`scripts/orch`)

A console driver for local tests — runs the agent loop directly (model
servers + LiteLLM proxy must be up, but not the web service). Uses the
checkout it lives in (`ORCH_HOME` overrides):

```bash
.venv/bin/python scripts/orch "What's the weather in Zurich?"
.venv/bin/python scripts/orch --max-cost 0.10 --tools web "cheap quick question"
.venv/bin/python scripts/orch --list-tools          # registry dump, no servers needed
.venv/bin/python scripts/orch --trace <run_id>      # replay a run's trace
.venv/bin/python scripts/orch --details "…"         # per-tool call/error/latency tally
```

Other flags: `--max-iterations`, `--max-wall-clock`, `--share-private`,
`--json-output` (full result dict for scripting). For HTTP-level scripting
use the API above instead — the CLI is the same-process shortcut.
`--doctor` doesn't run the agent at all: it validates the install (env
file, paths, ports, proxy, DBs, GPU, linger, units) with fix hints.

Sibling scripts: `scripts/pull-model` (interactive HuggingFace GGUF
downloader), `scripts/setup.sh` / `scripts/quickstart.sh` (installers —
see Quick start / Setup).

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
| `chains/` | named multi-step pipelines the model runs via `chain.run` |
| `prompts/` | `orchestrator-gate.md` — the live system prompt (~850 tok) |
| `config/` | `runtime.yaml` (main config), `litellm.yaml` (proxy config SEED — rendered to `/srv/data/litellm.yaml`), `quick-replies.yaml`, chat templates |
| `presets/` | factory llama-server presets (seed the DB catalog; edit via admin UI afterwards) |
| `scripts/` | `orch` CLI, `start-model.sh` (preset launcher), dev benchmarks |
| `systemd/` | user units (installed verbatim via `cp`) |
| `example_configs/` | adapt-and-install templates: `orchestrator.env.example` (secrets/paths/ports), `nginx.conf.example` (reverse proxy) |
| `docs/` | API contract, upgrade guide, recommended models, testing-harness guide |
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
  `example_configs/orchestrator.env.example`). Never commit it.
- **Data** — lives outside the repo (`/srv/data`): chats.db, users.db,
  trace.db, presets.db, rag.db, uploads, outputs, projects, and `custom/`
  (Studio-created skills/chains/connectors/tools, layered over the built-ins).

## Security notes

Posture in one paragraph: all HTTP is behind a deny-by-default auth middleware
(sessions are HMAC-signed cookies; `/api/admin/*` needs `is_admin`); passwords
are PBKDF2-HMAC-SHA256 (200k) with optional TOTP; all SQL is parameterized;
file tools are confined to the run's workspace; URL tools resolve and block
loopback/link-local/CGNAT targets and re-check every redirect hop;
`deliver.files` only hands over workspace files; HTML/SVG downloads are served
with `Content-Security-Policy: sandbox`. The LiteLLM proxy binds 127.0.0.1
only, so `LITELLM_MASTER_KEY` is optional for localhost-only installs — set it
if the proxy is ever exposed beyond localhost.

Accepted risks — deliberate tradeoffs, known and not (yet) fixed:

- **Prompt injection is the real threat model.** Web content the agent reads
  can steer the model. The confinement above limits the blast radius, but a
  steered agent can still act *within* a user's workspace and tools. Treat
  confirmation prompts as the last real gate, not a formality.
- **`auto_confirm` is client-supplied.** Any authenticated user (and scheduled
  runs) can bypass confirmation gating for their own runs. The privacy gate
  (what may leave the box) is *not* bypassable this way.
- **`ORCH_WEB_TOKEN` is full admin**, non-expiring and unscoped. It's the
  automation path; rotate it (env change + restart) if it may have leaked.
- **Login oracle / lockout DoS.** A correct password with 2FA enabled gets a
  distinct `totp_required` reply (confirms the password), and the per-account
  throttle (5 fails → 300 s lock) lets anyone who knows a username keep that
  account locked. Accepted for a small self-hosted instance.
- **DNS-rebinding TOCTOU.** The SSRF guard validates resolved IPs before
  connecting, but a hostname can re-resolve differently at connect time.
  Closing that fully needs connect-time IP pinning, which httpx makes
  invasive.
- **`code.deps` option injection.** Model-chosen "package" names are passed
  to pip/uv raw; a `--index-url` entry could redirect installs. Confirmation-
  gated; check the package list before approving.
- **No-firejail fallback.** `code.execute`/`code.run` run unsandboxed (logged)
  if firejail is missing. This box has `/usr/bin/firejail`; other deployments
  should install it.
- **Admin is a trusted role.** An admin can edit preset fields that flow into
  launcher commands and process configs — admin access ≈ host access by
  design. Admin-only XSS self-interpolation in the admin UI is out of scope.

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
- **Chains** (`tools/chain/`, `chains/*.yaml`) — named, reusable multi-step
  pipelines: sequential sub-agent + local prompt steps wired with
  `{{input}}` / `{{steps.<id>.output}}` placeholders, run via `chain.run`.
  Prompt steps are local-only so a chain can never bypass the cloud
  privacy/approval gate.
- **MCP bridge** (`tools/mcp/`) — `mcp.list`/`mcp.call` connect to Model
  Context Protocol servers (stdio subprocesses or HTTP endpoints) from
  `tools.mcp.servers` in runtime.yaml. Confirmation-gated per call by default,
  results private, stdio env scrubbed of secrets. Needs the optional `mcp`
  package (requirements-tools.txt).
- **Studio** (admin tab, `web/routes_studio.py`) — the admin creates custom
  skills, chains, API connectors and python tools in the browser, with
  AI-assisted drafting (local model guided by the writing-great-skills skill).
  Custom artifacts live in `$ORCH_DATA/custom/{skills,chains,connectors,tools}`
  and are layered over the built-ins (custom wins on name clash; survives
  git-pull deploys). Connectors are declarative YAML HTTP tools — no code,
  credentials only as env-var references. Python tools run with orchestrator
  privileges (admin-trusted) and take effect on restart. Everything is
  exportable/importable as `.jaypack` zips (`runtime/jaypack.py`) for sharing
  between JayNet installs.

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

## Versioning

SemVer. Single source of truth: `runtime/__init__.py` (`__version__`),
surfaced in `GET /api/health` and the admin Status page; releases are git
tags (`v0.9.0`). Current: **0.9.x** — feature-rich and daily-driven, but the
contracts below aren't frozen yet.

**1.0 = the public open-source release.** It means a stranger can install,
run and rely on JayNet:

- **Stable API contract** ✅ — `docs/api.md` defines the native-client
  surface and the change policy (additive-only within a version; breaking =
  minor bump + CHANGELOG), pinned by `tests/test_api_contract.py`.
- **Stable config & data** ✅ — `docs/upgrading.md`: DB schemas auto-migrate
  additively on boot (rollback-safe), the Studio custom layer lives outside
  the git tree, breaking changes land in `CHANGELOG.md`.
- **Installable from scratch** — README takes you from clone to running
  services without tribal knowledge (a ~15-min
  [minimal quick start](#quick-start-minimal-15-min) plus the full setup);
  no hardcoded hostnames/IPs/paths.
- **Repo hygiene** — git history swept for secrets ✅ (2026-08: no keys or
  tokens ever committed; early history holds only harmless personal files),
  license ✅ (MIT).

Until then the 0.9.x line is contract-hardening and polish, not new features.

## Inspirations & prior art

Where some of the ideas came from:

| Source | What we took from it |
| --- | --- |
| [arxiv.org/abs/2601.22037](https://arxiv.org/abs/2601.22037) — "Optimizing Agentic Workflows using Meta-tools" (AWO) | Profile-guided tool-call sequence mining from execution traces → `trace.mine`, the AWO-style recurring-sequence miner over `trace.db` that finds composite tool patterns (e.g. `ops.status→serve.health`, `fs.find→fs.read`) bundleable into meta-tools. The paper's "dominant prefix" insight (session-init routines at ~98% utilization) mapped directly onto the plumbing patterns. |
| [arxiv.org/abs/2601.01885](https://arxiv.org/abs/2601.01885) | Salience memory: salience-weighted compaction, pinned tool results surviving compaction. |
| [arxiv.org/abs/2607.05391](https://arxiv.org/abs/2607.05391) — "LLM-as-a-Verifier" | `verify.score` / `verify.rank`: logit-expectation over single-token grades instead of a judge's emitted token — continuous, tie-free scores, scaled along granularity / repeats / criteria. |
| [github.com/masamasa59/ai-agent-papers](https://github.com/masamasa59/ai-agent-papers) — agent-papers taxonomy | Harness engineering as its own discipline, versioned skill libraries (→ `skills/`), structured episodic memory over flat vector stores (→ `memory.*` + `kg.*`), execution-trajectory logging as foundational (→ `trace.db`). |
| [looprails.dev](https://looprails.dev) — "Agentic Loops in the Wild" | The verifier is the central variable: successful agents use external, ungameable verifiers (compilers, test suites). Shaped `verify.*`: wire loop decisions to `test.run`/`code.run` results, not model self-assessment; sandbox self-modification; cap iterations up front. |
| [github.com/Sahir619/fable-method](https://github.com/Sahir619/fable-method) | The Fable methodology — triviality gate, classify→define done→evidence→decide→act→verify→report, intent gate, fraud detection — adapted into the `fable-method`, `fable-loop`, `fable-judge` skills. |
| [Karpathy's LLM-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) | `/llmwiki`: an LLM-maintained persistent wiki (`index.md` + `log.md` + topic pages) as compiled knowledge, complementing RAG's raw sources. |
| "Get things done the engineering way" skill collections | `grill-me` (replaced the quick-settings toggle), `writing-great-skills` (→ `/wgs`), and the diff-based two-axis code review ported as `skills/diff-review` via `ctx.spawn`. |
| OpenRouter / Z.ai docs | Provider comparison, GLM-5.2 specs (744B MoE, 40B active, 1M ctx), endpoints, pricing → cloud-model consolidation. |

Development history (earlier sessions): Session 1 — core architecture,
LiteLLM, tool registry, agent loop, trace logging. Session 2 — branding,
`ask.user`, archives, admin UI, vision fix. Session 3 — model preset system,
ConcurrencyGate. Session 4 — brain+specialist posture, `council.debate`,
`verify.*`, `ops.*`, `trace.mine`, `boot_posture`.
