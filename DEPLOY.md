# JayNet Orchestrator — Deploy Guide

This is the **consolidated** build. It folds together every increment that was
previously shipped as a separate patch:

- self-service **account area** (`/account`) with usage/budget, password change,
  per-user **session revocation**, and per-user **API tokens**
- **voice channel** (`POST /api/voice`) for native/voice clients
- **project-from-chat** promotion (web button + voice command)
- live **per-tool running indicators** in the activity log
- **in-run budget checkpointing** (a one-time "land the plane" notice) plus the
  `coding-projects` skill and a plan-first prompt
- project **file deletion** UI and visible **chat↔project links**
- **vision**: image attachments forwarded to the brain when it's vision-capable,
  driven by a `llama-serve.sh` **preset** (`brain_preset` / `ORCH_BRAIN_PRESET`)

From here on you can deploy by dropping this tree in and restarting the service;
there is no patch stack to reapply.

---

## 1. Topology

```
client ── HTTPS ──> nginx ──> orchestrator web (uvicorn :8071)
                                   │  reasoning loop, tools, SSE
                                   ▼
                            LiteLLM proxy (:4000)   ← cost accounting, alias
                                   │
                                   ▼
                            llama-server "brain" (:8090)   ← the GGUF model
```

The orchestrator only ever talks to **LiteLLM** (`orchestrator.litellm_base`,
default `http://127.0.0.1:4000`). LiteLLM's config maps the alias
(`orchestrator.model`, default `local-orchestrator`) to the brain at `:8090`.
The orchestrator never dials the brain's port directly — so switching brain
presets/ports only needs the **preset** (what `llama-serve.sh` binds) and
**`litellm.yaml`** (what LiteLLM dials) to agree.

Ports: **8071** web · **4000** LiteLLM · **8090** brain (all bound to localhost
except the web port, which the reverse proxy reaches).

---

## 2. Layout

Everything lives at `/srv/orchestrator/`:

```
runtime/        agent loop, budget, registry, selector, serve_preset, trace, skills
tools/          18 tool namespaces (fs, web, rag, git, code, job, llm, serve, …)
web/            FastAPI server, auth, store, projects, static console
prompts/        orchestrator.md (system prompt)
skills/         loadable SKILL.md playbooks (incl. coding-projects)
config/         runtime.yaml, litellm.yaml, costs
systemd/        unit files
data/           created at runtime: *.db, uploads/ outputs/ projects/ serve/
.venv/          runtime virtualenv (you create this)
```

---

## 3. First-time install

### 3.1 Runtime venv

```bash
cd /srv/orchestrator
uv venv .venv                       # Python 3.13 recommended
uv pip install --python .venv/bin/python -r requirements.txt \
                                            -r requirements-web.txt \
                                            -r requirements-test.txt
```

### 3.2 Playwright (only if you use `web.render`)

```bash
uv pip install --python .venv/bin/python playwright
.venv/bin/python -m playwright install chromium      # NOT --with-deps on Arch
```

### 3.3 LiteLLM (separate venv — never share with the runtime venv)

```bash
uv venv /srv/orchestrator/litellmenv
uv pip install --python /srv/orchestrator/litellmenv/bin/python -r requirements-litellm.txt
```

`config/litellm.yaml` must map the orchestrator's alias to the brain, e.g.:

```yaml
model_list:
  - model_name: local-orchestrator              # == orchestrator.model
    litellm_params:
      model: openai/Qwen3.6-35B-A3B-...          # the brain's --alias
      api_base: http://127.0.0.1:8090/v1         # == preset HOST:PORT
      api_key: none
```

Run it **stateless** (no `database_url`); cost is tracked in `trace.db`. Every
`model_name` you route also needs a row in `runtime.yaml`'s `costs:` table or
`budget.add_usage()` silently bills it at $0 (local model is free — that's fine).

---

## 4. The brain (llama-server)

Serve it with the launcher, headless, from a preset:

```ini
# systemd/llama-orchestrator.service (ExecStart)
EnvironmentFile=%h/.config/orchestrator.env
ExecStart=/srv/llama/llama-serve.sh --preset ${PRESETS}
```

The preset carries the model path, vision projector, sampling, KV type, and
(for MTP models) the `--spec-type draft-mtp` flags. **Make the preset's
`PORT` match `litellm.yaml`'s `api_base`** (e.g. `8090`) and prefer
`HOST=127.0.0.1`. For an orchestrator brain, the preset should set
`SYSTEM_PROMPT=` empty (the orchestrator sends its own), `PRESENCE_PENALTY=0`,
and a low `TEMP` (~0.3) for tool-call reliability.

---

## 5. Service config (`orchestrator.env` + units)

The web unit reads `EnvironmentFile=%h/.config/orchestrator.env` and no longer
hardcodes the install path — `orchestrator.env` is the single source of truth for
it. A ready template ships at `systemd/orchestrator.env`; install it (mode 600,
it holds tokens) and edit:

```sh
install -Dm600 systemd/orchestrator.env ~/.config/orchestrator.env
$EDITOR ~/.config/orchestrator.env
```

Key lines (plain `KEY=VALUE`, no shell expansion):

```sh
# ~/.config/orchestrator.env
ORCH_HOME=/srv/orchestrator                               # install path anchor
ORCH_CONFIG=/srv/orchestrator/config/runtime.yaml         # read by web.server
PYTHONPATH=/srv/orchestrator                              # so `import web.server` resolves
PATH=/srv/orchestrator/.venv/bin:/usr/local/bin:/usr/bin:/bin:/opt/rocm/bin
ORCH_WEB_TOKEN=<long-random-token>        # Bearer token for API/native clients
ORCH_ADMIN_PASSWORD=<set-this>            # else a random one is generated & logged once
ORCH_BRAIN_PRESET=/srv/llama/presets/<your-brain>.conf   # app: vision detection
PRESET=/srv/llama/presets/<your-brain>.conf              # launcher: which brain to serve
LITELLM_MASTER_KEY=<key>                  # only if serve.* registers runtime aliases
CODER_PRESET=/srv/llama/presets/<your-coder>.conf        # code.delegate model (GPU 1, :8080)
```

`PRESET` (launcher) and `ORCH_BRAIN_PRESET` (app metadata) are two consumers of
the same brain `.conf`; both are optional (built-in defaults). The coder uses its
own `CODER_PRESET` — never reuse `PRESET` for it (that's the brain's).

A `--user` service does **not** inherit your login `PATH`, so set it here — it
must reach the venv plus the binaries the tools shell out to (`git`, `firejail`
for `code.run`, `ctags` for `code.symbols`, `ruff`/`mypy` for `lint.run`,
`uv`/`pip` for `code.deps`, `rocm-smi` for `gpu.status`). If you install outside
`/srv/orchestrator`, change every path line here **and** `WorkingDirectory` +
`ExecStart` in the unit (systemd can't expand env vars in the ExecStart binary path).

Note `ORCH_BRAIN_PRESET` (env) overrides `orchestrator.brain_preset` in
`runtime.yaml`; either one works. It is read **only** to learn the served model
name and whether vision (an `MMPROJ`) is loaded — it does not change routing.

Edit `systemd/orchestrator-web.service` before installing:

- set `--forwarded-allow-ips=` to the **nginx host IP** (it currently says
  `PROXY_IP_HERE`); the unit already binds `0.0.0.0:8071` for the off-box proxy
- `UMask=0077` is already set so all data files are created private

Install and start (user services):

```bash
mkdir -p ~/.config/systemd/user
cp systemd/*.service ~/.config/systemd/user/
install -Dm600 systemd/orchestrator.env ~/.config/orchestrator.env   # if not already
systemctl --user daemon-reload
systemctl --user enable --now litellm-proxy llama-orchestrator orchestrator-web
```

If you want the user services to keep running after you log out:
`loginctl enable-linger $USER`.

### Optional: dedicated coder for `code.delegate`

`llama-coder.service` serves a coder model on **GPU 1 (:8080)** via
`scripts/start-coder.sh`, mirroring the brain's launch path. It uses its own
`CODER_PRESET` (+ `CODER_MODEL_PATH`) and is pinned to GPU 1 so it never contends
with the brain on GPU 0. Because the delegated sub-agent makes tool calls, the
coder is served with the tool-calling template by default.

```bash
# 1) set CODER_PRESET / CODER_MODEL_PATH in ~/.config/orchestrator.env, then:
systemctl --user enable --now llama-coder
# 2) map a LiteLLM alias -> http://127.0.0.1:8080, add a $0 costs: row for it
# 3) set tools.code.delegate.model in runtime.yaml to that alias, restart web
```

Keep it resident — a restart is a full ~90s model reload that every delegation
would otherwise pay.

The first boot auto-creates an admin (`ORCH_ADMIN_USER`, default `admin`) with
`ORCH_ADMIN_PASSWORD` (or a generated password written to the journal once).
Find it with `journalctl --user -u orchestrator-web | grep "created admin"`.

---

## 6. Permissions (one-time hardening)

The DBs hold password hashes, TOTP secrets, API-token hashes, and full
conversation/tool content. Lock the data dir down:

```bash
cd /srv/orchestrator
chmod 700 data
chmod 600 data/*.db data/*.db-* 2>/dev/null || true
chmod -R go= data/uploads data/projects data/outputs 2>/dev/null || true
```

Keep the brain and LiteLLM bound to `127.0.0.1`. Set `ORCH_WEB_TOKEN` (or
firewall `:8071` to the proxy) since the web port is no longer localhost-only.

---

## 7. nginx (reverse proxy)

The proxy runs on another host and upstreams to the web service. Essentials:

- TLS cert must cover **`ask.jaynet.ch`** (with `orch.jaynet.ch` as an alias);
  `server_name ask.jaynet.ch orch.jaynet.ch;`
- upstream the LAN address, e.g. `proxy_pass http://192.168.124.102:8071;`
- **pass Authorization through** (default nginx behavior — don't strip it; the
  API and native clients send `Bearer` tokens)
- **`proxy_buffering off;`** on the streaming routes (`/api/stream/…`,
  `/api/chat`, `/api/voice`) or SSE will stall
- `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` and match the
  service's `--forwarded-allow-ips`

---

## 8. Key `runtime.yaml` settings

- `orchestrator.model` / `litellm_base` — the LiteLLM alias + proxy address
- `orchestrator.brain_preset` — path to the active `llama-serve.sh` preset
  (or set `ORCH_BRAIN_PRESET`); `orchestrator.vision: null|true|false` forces
  the capability if you don't want auto-detect
- `budgets.warn_fraction: 0.8` — at this fraction of any ceiling the run gets a
  one-time checkpoint notice (`0` disables)
- `voice:` block — model/budget/persona for `POST /api/voice`
- `costs:` — one row per LiteLLM `model_name` you route

DB schema migrations (trace.db owner/token columns, users.db session_epoch +
api_tokens + budget_defaults) run **automatically and are backward-compatible**;
no manual migration step.

---

## 9. Verify

```bash
# service up
curl -s -H "Authorization: Bearer $ORCH_WEB_TOKEN" http://127.0.0.1:8071/api/me | jq
#   -> {... "vision": true/false, "brain_model": "<file>.gguf"}

# brain reachable through LiteLLM
curl -s http://127.0.0.1:4000/v1/models | jq '.data[].id'
```

Then load the web console (hard-refresh, Ctrl-Shift-R, after any static change),
sign in, and run a prompt. If `vision` is true, attaching an image forwards it to
the brain; if false, the agent is told it can't see images and to use a tool.

---

## 10. Upgrading later

Because this is consolidated, future changes are drop-in: replace the changed
files (or the whole tree), then `systemctl --user restart orchestrator-web`, and
hard-refresh the browser for static assets. Migrations remain automatic.

---

## 11. Optional performance follow-ups (brain-side, not orchestrator)

These were discussed and are independent of the orchestrator deploy:

- run a higher quant than IQ4 (you have 64 GB VRAM) — `Q5_K_M`/`Q6_K` improves
  tool-call fidelity; `f16` KV is lossless and keeps HIP fused flash-attn engaged
- for an `-MTP-` model, enable `--spec-type draft-mtp --spec-draft-n-max 2`
  (the launcher auto-enables this and forces `f16` KV); gains on the 35B-A3B MoE
  are modest (~7–16%), not the dense ~2×
- vision encoder placement: GPU by default; `MMPROJ_OFFLOAD=off` runs it on CPU
  as a workaround for the gfx1201 "GPU never idles" bug (slower image encode)

Verify MTP and the CLIP/vision path actually work on your gfx1201 HIP build —
the upstream benchmarks for both are CUDA/Metal.
