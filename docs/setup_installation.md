# Guided install (setup.sh)

The permanent install: `scripts/setup.sh` turns a clone into a running
service stack — Python envs, env file with generated secrets, systemd
`--user` units, linger. Idempotent (safe to re-run) and interactive.
For the throwaway try-out use `scripts/quickstart.sh` (README quick start);
for full control there's the [manual install](manual_installation.md).

Assumptions: Linux with systemd `--user` (on Windows: WSL2, follow this
as-is; macOS has no services — use quickstart). Nothing is fixed to a path:
the install root is wherever you clone, data/models dirs are asked for.

## 1. Prerequisites

`git`, `python3` (≥ 3.10) and [`uv`](https://docs.astral.sh/uv/):

```bash
# Arch Linux
sudo pacman -S git python uv

# Ubuntu / Debian
sudo apt install git python3
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv
```

## 2. Clone and run

```bash
git clone https://github.com/jspawn/jaynet_orchestrator.git ~/jaynet-orchestrator
cd ~/jaynet-orchestrator
scripts/setup.sh
```

Flags: `--yes` (non-interactive, accept defaults, services **not** started) ·
`--start` (enable + start the units at the end, no question) ·
`--with-tools` (also install `requirements-tools.txt` — charts, browser
rendering).

## What the script does

1. **Prereq check** — git, python3 ≥ 3.10, uv; prints the package-manager
   line for whatever is missing and stops.
2. **Asks for two dirs** — the data dir (chats, users, projects, wiki,
   uploads; default `~/jaynet-data`) and the models dir (GGUFs; default
   `~/jaynet-models`). Write access is checked; re-ask on failure. An
   existing env file's `JAYNET_DATA` / `JAYNET_MODELS` always win (re-runs).
   **Keep the data dir outside the clone** — live databases in a git tree
   break your git workflow.
3. **Python envs** — `.venv` (core + web) and `litellmenv` (the LiteLLM
   proxy gets its own venv: its `[proxy]` extra pins versions that would
   fight the runtime venv).
4. **Env file** — writes `~/.config/jaynet.env` (mode 600) with generated
   secrets: session secret, web token, `LITELLM_MASTER_KEY`, and a
   first-boot admin password that is **printed once** at the end. Paths in
   the file are adjusted to your clone location. An existing env file is
   left untouched (a warning fires if it still has `<...>` placeholders).
5. **systemd units** — copies `systemd/*.service` to
   `~/.config/systemd/user/`, reloads, and enables **linger**
   (`loginctl enable-linger`) so the services survive logout. Then asks to
   enable + start `litellm-proxy` and `jaynet-web` (unless `--yes`/`--start`).

## What stays manual

- **llama.cpp** — build for your hardware:
  [manual_installation.md → Preparing llama.cpp](manual_installation.md#preparing-llamacpp).
  Shortcut for CPU-only trials: drop a prebuilt release binary into `bin/`
  (what quickstart does; `LLAMA_BIN` env overrides the lookup).
- **Models** — download GGUFs into your models dir:
  `scripts/pull-model <hf-repo>` (interactive picker). License-clean picks:
  [models.md](models.md). The shipped default catalog: brain = Qwen3-4B
  (GPU 0 or CPU), embed + rerank = Qwen3 0.6B (CPU).
- **Presets** — adjust `presets/*.conf` to your hardware (ctx size, KV
  quant, VRAM) and the device placement in **Admin → Presets**.

## First run

If the services aren't running yet:

```bash
systemctl --user enable --now litellm-proxy jaynet-web
```

Browse to `http://<host>:8071` and log in as `admin` with the password the
installer printed (regenerate any time: set `JAYNET_ADMIN_PASSWORD` in the
env file and delete the users DB, or change it after login). Then remove the
`JAYNET_ADMIN_*` lines from `~/.config/jaynet.env`.

Post-install checklist: change the admin password (Account → Security) ·
set your location + timezone (Account → Settings) · create a per-user API
token if you'll use the CLI/API · tune quick settings (nerd mode, theme, run
preferences) to taste.

Validate the whole setup:

```bash
scripts/orch --doctor    # env file, paths, ports, proxy, DBs, GPU, linger
```

Optional pieces: cloud API keys in the env file (`llm.call` escalation),
`firejail` (code sandbox), a SearXNG container (`web.search`), system
Chromium or a Playwright CDP container (`browser.*`), ROCm/CUDA drivers.
Reverse proxy for remote access:
[manual_installation.md → Reverse proxy](manual_installation.md#reverse-proxy-optional-for-remote-access).
Uninstall:
[manual_installation.md → Uninstall](manual_installation.md#uninstall).
