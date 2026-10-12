# AI-assisted setup

*Operators — hand this page to an AI coding assistant and let it drive the
install interactively.*

This page is a **paste-ready playbook** for an AI CLI (Kimi Code, Claude
Code, Codex, …) running on the machine you want to install JayNet on. It
walks the assistant through packages, models, services and optional
plugins, asking you before anything that costs disk, money or sudo.

## How to use it

1. Clone the repo where you want it to live and open your AI CLI there:
   ```bash
   git clone https://github.com/jspawn/jaynet_orchestrator.git ~/jaynet-orchestrator && cd ~/jaynet-orchestrator
   ```
2. Paste the playbook below (everything in the fenced block) into the
   assistant, or point it at this file: *"Read docs/ai_assisted_setup.md
   and follow the playbook."*
3. Answer its questions. It should work phase by phase and stop on any
   error it can't explain.

The manual paths stay untouched: `scripts/quickstart.sh` (try-out),
`scripts/setup.sh` + [setup_installation.md](setup_installation.md)
(guided), [manual_installation.md](manual_installation.md) (by hand).

## The playbook

~~~text
You are setting up JayNet — a local-first LLM orchestrator (Python/FastAPI
web console + llama.cpp model swapping + LiteLLM proxy) — on this machine.
The repo is already cloned; your working directory is the repo root.

GROUND RULES
- Interactive: work phase by phase, tell me what each phase does in one
  sentence, and wait for my go-ahead before starting the next one.
- Never run sudo, installs, downloads >100 MB, or anything destructive
  without asking first. Show the exact command before running it.
- Never write secrets into the repo. Secrets live only in
  ~/.config/jaynet.env (mode 600).
- The data dir must NEVER be inside this checkout (live DBs in a git tree
  break git). Default ~/jaynet-data / ~/jaynet-models.
- After every command, check its output actually shows success before
  moving on. On an error you can't explain in one line, stop and ask.
- The docs are the contract: docs/manual_installation.md has the full
  reference if anything below conflicts with what you observe.

PHASE 0 — Base packages
Check: git, python3 (>= 3.11, hard gate), uv, curl, unzip. Install
whatever is missing via my package manager (apt/dnf/pacman — ask which
distro this is if unsure). On Ubuntu/Debian also install libgomp1, on
Arch gcc-libs (the prebuilt llama-server needs OpenMP).

PHASE 1 — Choose the install path (ask me)
- Try-out (recommended if I'm new): scripts/quickstart.sh — one small
  model, one terminal, no services. Then jump to PHASE 7.
- Permanent install: scripts/setup.sh — venvs, env file with generated
  secrets, systemd --user units. Continue with PHASE 2.

PHASE 2 — Guided installer
Run scripts/setup.sh (add --with-tools if I want browser tools/chart
rendering; it will offer a headless Chromium). It writes
~/.config/jaynet.env with generated JAYNET_SESSION_SECRET /
JAYNET_WEB_TOKEN / LITELLM_MASTER_KEY and a first-boot admin password —
note the password for me; it is shown once. It installs systemd --user
units and enables linger. Do NOT start services yet if models are missing.

PHASE 3 — llama.cpp (skip if quickstart already fetched a binary)
Ask what hardware I have (AMD/NVIDIA GPU + VRAM, or CPU-only). Options:
- Quick: re-run scripts/quickstart.sh --yes just for the verified prebuilt
  CPU binary (lands in bin/llama-server).
- Real GPU build: build_tools.sh from the companion repo
  github.com/jspawn/helper_scripts (clone it OUTSIDE this repo):
    ./build_tools.sh llama            # backend menu (Vulkan default)
    ./build_tools.sh llama rocm       # AMD (ROCm >= 6.4.1, user in video+render)
    ./build_tools.sh update           # later: rebuild everything to latest
  Then register the binary in Admin → Models → Servers → Binaries with the
  right device_env (HIP_/CUDA_/GGML_VK_VISIBLE_DEVICES).
Sanity-check: llama-server --list-devices shows my GPU(s).

PHASE 4 — Models (ask about my VRAM/RAM budget first)
Download with scripts/pull-model <hf-repo> (interactive file picker;
HF_TOKEN in the env file only for gated repos). Suggest per tier:
- Minimal / 8 GB RAM, no GPU: Qwen/Qwen3-1.7B-GGUF (quickstart default).
- 8–12 GB VRAM: brain Qwen/Qwen3-4B-GGUF (shipped default preset),
  embed Qwen/Qwen3-Embedding-0.6B-GGUF + rerank Qwen/Qwen3-Reranker-0.6B-GGUF
  on CPU (RAG helpers, cheap).
- 24+ GB VRAM: add a specialist (e.g. a 27–32B coder GGUF) or a bigger
  MoE brain (Qwen3-30B-A3B preset ships as example).
License-clean picks are listed in docs/models.md. Match my choice to the
presets in Admin → Models → Presets (the catalog seeds into
$JAYNET_DATA/presets.db on first boot — afterwards edit it in the UI, YAML
edits no longer apply).

PHASE 5 — First config
- config/runtime.yaml: orchestrator.litellm_base must match the proxy port
  (default 4000); only touch if I moved ports.
- Start: systemctl --user enable --now litellm-proxy jaynet-web
- Verify: scripts/orch --doctor must be all-OK (env file, paths, ports,
  proxy, DBs, GPU, linger). Fix what it flags before continuing.
- Browse to http://<host>:8071, log in as admin with the installer
  password, then remove the JAYNET_ADMIN_* lines from ~/.config/jaynet.env.

PHASE 6 — Optional plugins (ask me per plugin, one-line pitch + cost;
all ship disabled and toggle in Admin → Harness → Plugins)
- pageindex — long-PDF tree index (pip install pageindex; keep its model a
  LOCAL alias or document text leaves the box).
- omnivoice — local text-to-speech (tts-server via build_tools.sh + ~1 GB
  GGUFs; weights are CC-BY-NC — mention that).
- imagegen — local text-to-image (sd-server via build_tools.sh + ~12 GB
  models; hibernates the specialist slot during generation).
- h5i — fast red-team-capable browser (external binary from h5i.dev;
  installs to ~/.local/bin with no sudo, JayNet finds it there).
- graphify — per-project knowledge graphs (pip install graphifyy).
- benchlab — import Terminal-Bench/GAIA eval tasks (podman for full mode).
- jev / clm — decision-classifier experiments; ship off on measured
  verdicts, skip unless I'm curious.
- Voice input (STT): whisper.cpp via build_tools.sh whisper cpu + the
  stt-whisper-large-v3-turbo preset assigned to the stt boot slot.
Restart the web service after pip installs; binary plugins apply live.

PHASE 7 — Validate + first steps
- scripts/orch --doctor clean; Admin → Status & Usage → Overview green.
- Chat smoke test: ask it to write and run a small script — watch the
  delegate → verify flow work.
- Remind me: change the admin password (Account → Security), set
  location/timezone (Account → Settings), opt into the backup timer
  (systemctl --user enable --now jaynet-backup.timer) if I want daily
  backups, and re-run model.measure per active preset (Admin → Models →
  Presets) so the fit-aware scheduler has real numbers.

When done, summarize: what was installed, which ports are in use, which
plugins are enabled, and what's left for later.
~~~
