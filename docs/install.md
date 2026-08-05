# Manual install (advanced)

The full, by-hand setup — for when `scripts/quickstart.sh` /
`scripts/setup.sh` (see the README quick start) are too opaque or you need
a custom layout.
Assumptions: Linux, systemd `--user` services, AMD GPUs via ROCm (NVIDIA/CPU
works too — adjust the presets and the llama.cpp build). There are no fixed
paths: the install root is wherever you clone (README suggests
`~/jaynet-orchestrator`), data defaults to `$ORCH_HOME/data` — overridable
via `ORCH_HOME` / `ORCH_DATA` / `ORCH_MODELS` in the env file (see
`runtime/paths.py`, the single source of truth for paths; the author's own
setup uses `/srv/orchestrator` + `ORCH_DATA=/srv/data`).

Everything under `config/`, `presets/`, `prompts/`, `systemd/` and
`example_configs/` is a **working example deployment** — adapt it to your
host (the shipped prompt names wolf's models/cloud aliases; edit it or
point `orchestrator.system_prompt` at your own). Secrets never live in the
repo: config files reference env var *names*, values only enter via the env
file (step 4).

**Automated:** `scripts/setup.sh` covers steps 0 + 3–6 (prereq check, venvs,
env file with generated secrets, systemd units, linger; `--start` to launch
services, `--with-tools` for the optional extras). It asks for the data and
models dirs up front (defaults `~/jaynet-data` / `~/jaynet-models`, write
access checked; `--yes` takes the defaults silently) and writes them as
`ORCH_DATA` / `ORCH_MODELS` into the env file it generates. Steps 1–2
(llama.cpp, models) stay manual — or use `scripts/pull-model` for the
downloads. After anything install-related, `scripts/orch --doctor` validates
the whole setup. The manual path:

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
   [models.md](models.md)) and point the presets at them. The shipped
   default catalog: brain = Qwen3-4B (GPU 0 or CPU), embed + rerank = Qwen3
   0.6B (CPU). Adjust `presets/*.conf` to your hardware (ctx size, KV quant,
   VRAM) and the device placement in admin → Presets — e.g. one big brain
   split across all GPUs with the specialist on CPU or stopped.
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
   self-seeds into `$ORCH_DATA/presets.db`; manage it in **Admin → Presets**.
   Check **Admin → Status** for service health — or run
   `scripts/orch --doctor` for a full install validation (env file, paths,
   ports, proxy, DBs, GPU, linger).

   Post-install checklist: change the admin password (Account → Security) ·
   set your location + timezone (Account → Settings — used for local queries
   and freshness) · create a per-user API token if you'll use the CLI/API ·
   tune quick settings (nerd mode, theme, run preferences) to taste.

Optional pieces: see step 0 — plus cloud API keys in the env file for
`llm.call` escalation.

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

## Reverse proxy (optional, for remote access)

The console listens on `:8071` plain HTTP; put nginx in front for TLS and
remote access. A complete, annotated example lives at
[`example_configs/nginx.conf.example`](../example_configs/nginx.conf.example). The four things that
matter:

1. **SSE must not be buffered** (`proxy_buffering off` on `/api/stream/`),
   or live tokens arrive in clumps; long read timeouts for long runs.
2. **Forward the headers** (`Host`, `X-Forwarded-For/Proto`) — and set the
   trusted proxy IP via `ORCH_FORWARDED_ALLOW_IPS` in the env file (the web
   unit passes it to uvicorn's `--proxy-headers --forwarded-allow-ips`).
   With TLS in place, also set `web.cookie_secure: true` in
   `config/runtime.yaml` so the session cookie is marked Secure (it defaults
   to false — a Secure cookie is never sent over the plain-HTTP direct
   console, so login would silently break).
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
#    Studio custom layer. Back up first if unsure (upgrading.md).
#    Default shown; use your ORCH_DATA if you overrode it.
rm -rf /srv/orchestrator/data
```

Leftovers to remove by hand if you set them up: the nginx vhost + Let's
Encrypt certs (`example_configs/nginx.conf.example`), `loginctl disable-linger "$USER"`,
and the host-side pieces outside this repo — llama.cpp builds, GGUF models,
a SearXNG container.
