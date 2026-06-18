# JayNet Orchestrator — project handover

Paste this into a new chat to bring an assistant up to speed. It describes a
personal learning project: a local LLM **agent loop** ("JayNet Orchestrator")
running on my Arch workstation. I'm the sole user/developer.

## Machine & inference stack ("wolf")
- Dual **AMD Radeon AI PRO R9700** (gfx1201, RDNA4, 32 GB each), Ryzen 9 7950X,
  MSI MEG X670E Ace. Arch Linux, **ROCm 7.x**, Python 3.13.
- Env (sourced via `rdna4-env.sh`): `HIP_VISIBLE_DEVICES=0,1`,
  `GPU_MAX_HW_QUEUES=1`. Models in `/srv/models` (~179 GB GGUF).
- **Brain** model: Qwen3-35B MoE GGUF on **llama.cpp (HIP, gfx1201)**,
  `llama-server` on **:8090**, behind a **LiteLLM** proxy on **:4000**.
- Cloud providers configured in LiteLLM: Claude, Gemini, Qwen.
- **Inference-engine decision (keep llama.cpp):** vLLM/SGLang give little to a
  single-user sequential agent (their win is batched concurrency), and RDNA4
  support was immature as of 2026 — FP8 silently falls back to FP32, and a dual-
  R9700 TP=2 RCCL deadlock matches this exact box. Revisit if real concurrency is
  added (parallel sub-agents, `eval.compare` fan-out, multi-user) or once RDNA4
  FP8 stabilizes (then SGLang RadixAttention prefix-caching gets attractive). Easy
  A/B path: add a vLLM endpoint as a second LiteLLM alias, no orchestrator change.

## What it is
A bounded Level-1 agent loop deployed at **`/srv/orchestrator/`**: a plugin tool
registry, per-run budgets, SQLite tracing, namespace privacy gating, a confirmation
gate, a transport-neutral event sink, and a FastAPI + SSE web console.

Layout:
- `runtime/`: `loop.py` (`AgentRuntime.run(user_message, *, share_private,
  auto_confirm, tools, budget_overrides, run_id, on_event, confirm_provider,
  history, stream)`), `registry.py` (auto-discovers `tools/<ns>/*.py`, files
  starting `_` skipped), `tool_base.py` (`Tool` / `ToolResult` / `ToolContext`),
  `budget.py`, `trace.py` (tables `runs`, `events`), `events.py` (`EventBus`),
  `confirm.py` (`WebConfirmationProvider`), `selector.py` (cache-safe per-run tool
  selection).
- `tools/`: one folder per namespace (see below).
- `web/`: `server.py` (FastAPI + SSE), `store.py` (`ChatStore`, per-user `owner`),
  `auth.py` (`UserStore` + sessions + TOTP), `static/{index,login,admin}.html`.
- `config/runtime.yaml`, `prompts/orchestrator.md`, `scripts/orch` (one-shot CLI),
  `systemd/orchestrator-web.service`, `requirements{,-web,-litellm,-test}.txt`,
  `docs/testing-harness.md`, `LEARNING_GUIDE.md` (~5.5k lines; §12 = phases 5–11).

## Tools (40, all auto-discovered)
`llm.call`, `eval.compare`, `web.search`/`web.fetch`, `arxiv.search`/`arxiv.get`,
`code.execute` (firejail sandbox: stdlib-only, no net, seconds),
`job.start`/`status`/`logs`/`list`/`cancel` (detached, GPU, persistent — fire and
poll), `gpu.status`, `fs.read`/`list`/`grep`/`write`/`edit`,
`git.status`/`diff`/`log`/`show`/`add`/`commit`/`branch`,
`rag.index`/`search`/`collections`/`delete`,
`memory.append`/`search`/`get`/`list`/`delete`,
`kg.upsert_entity`/`add_relation`/`query`/`neighbors`/`remove_relation`,
and **`test.run`** (the self-test harness — see below).

- **Privacy:** `private_tool_namespaces: [rag, fs, test]` in config; `job`/`git`/
  `memory`/`kg` set `private` at class level. Private results can't be passed to
  the remote `llm.call` unless `share_private` is set for the run.
- **Confirmation-gated:** `job.start`, `job.cancel`, `git.add`/`commit`/`branch`,
  `fs.write`/`edit`, `rag.delete`, `memory.delete`, `kg.remove_relation`,
  `test.run`.

## Web console (current state)
Branded **"JayNet Orchestrator"**, single centered chat column. Each response
shows the streamed answer, a footer (`model · turns · tools · tokens · cost ·
time`, cost ticking live), and a collapsible activity one-liner; Qwen3 `<think>`
is routed to a reasoning disclosure, never into the answer. Multi-turn + token/
cost streaming are wired on the **web path only** (the CLI stays one-shot).

- **Right-side Tools panel:** per-tool enable/disable switches **grouped by
  namespace in collapsible groups** (caret + `enabled/total` count badge),
  all/none buttons. State persists per user (the *disabled* set) and is enforced
  server-side on every run — a disabled tool can't be forced back in.
- **Auth (`web/auth.py`):** username/password (pbkdf2-hmac-sha256, per-user salt),
  **stdlib HMAC-signed session cookies** (no `itsdangerous`/SessionMiddleware). One
  `auth_mw` middleware gates everything: `401` for `/api/*`, `302 → /login` for
  pages, admin-only gating for `/admin` + `/api/admin/*`. `ORCH_WEB_TOKEN` bearer
  still works for API/CLI (implicit admin). Chats are per-user (`owner` column).
- **2FA (TOTP, stdlib `hmac`, ±1 step window):** header **2FA** button → modal:
  enroll (pending secret + `otpauth://` URI → confirm first code → 10 one-time
  backup codes shown once) and self-disable (needs a current code). Login reveals a
  code field when the server replies `401 totp_required`. Backup codes are single-
  use. Admin can **reset 2FA** for a locked-out user.
- **Admin page (`/admin`, admin-only):** live system-prompt editor (writes the file
  + updates `runtime.system_prompt`, effective next run); service status (probes
  LiteLLM + any `web.services`, plus process info and DB sizes); run logs from the
  trace DB with click-through to a run's events; user management (add, reset pw,
  grant/revoke admin, delete, reset 2FA) with guards against deleting yourself or
  the last admin.

## `test.run` — the self-test harness (lets the agent test code like a dev)
Private + `requires_confirmation`. The agent passes a test as `test` (one file) or
`files` (a map, for multi-file suites + `conftest.py`); it runs in an isolated
workdir against a deps venv with the project root on `PYTHONPATH` (so tests can
`import web.server`, `runtime.*`, `tools.*`). The pattern: drive a FastAPI app
**in-process via httpx `ASGITransport`** with the model/externals mocked — no
network, no live server.
- **Quick mode** (default): bounded `pytest -q` subprocess, returns parsed
  `{passed, failed, errors, skipped, ok, returncode, duration_s, stdout, stderr}`.
- **Detached mode** (`detached=true`): hands the same command to `job.start`;
  returns a `job_id` — poll with `job.status`/`job.logs` (exit 0 = passed).
- Config `tools.test`: `python` (venv with pytest+pytest-asyncio+httpx+fastapi),
  `project_root`, `workdir_root`, `quick_timeout_s`, optional `sandbox_prefix`.
- `docs/testing-harness.md` has the canonical ASGI/mock example + idioms.

## Config & env essentials
- `config/runtime.yaml`: `orchestrator.{model,litellm_base,system_prompt}`,
  `costs` table, `trace.db_path`, `privacy.*`, `confirmation.*`,
  `web.{chats_db,users_db,cookie_secure,services}`, `tools.{web,code,job,test,…}`.
- Env: `ORCH_ADMIN_USER`/`ORCH_ADMIN_PASSWORD` (first-boot admin seed),
  `ORCH_SESSION_SECRET` (persist sessions; else `data/session.secret`),
  `ORCH_WEB_TOKEN` (API/CLI bearer — **bypasses login, so it skips 2FA**:
  break-glass, guard it), `ORCH_CONFIG` (config path).

## Operational notes
- **First boot** with an empty users table seeds an admin from the env vars, or
  generates a password and logs it once (`journalctl -u orchestrator-web | grep
  generated`). The seed only fires while the table is empty — set the password you
  want *before* first start; afterwards manage users from `/admin`.
- **Locked out / reset admin:** `cd /srv/orchestrator && python -c "from web.auth
  import UserStore; UserStore('data/users.db').set_password('admin','new-pw')"`
  (or `.create(...)` a new admin). No restart needed — the store reads per request.
- **2FA recovery:** backup codes, admin reset, edit `users.db`, or the bearer token.
- **Test venv:** `uv pip install -r requirements.txt -r requirements-web.txt -r
  requirements-test.txt` into the venv `tools.test.python` points at.
- Harden behind TLS: `cookie_secure: true`; disable proxy buffering on
  `/api/stream/`.

## Live checks only the box can do (the sandbox can't)
- Streaming usage (`include_usage`) actually returns from LiteLLM/providers so cost
  is non-zero; `gpu.status` parser on real ROCm; `rag` embed/rerank + `arxiv` HTTP;
  2FA codes accepted by a real authenticator (clock skew); admin status probes
  against the live `:4000`/`:8090`; `test.run` against the real venv.

## Docs status
`prompts/orchestrator.md` lists all tools incl. `test.run` (catalog + privacy +
confirmation). `LEARNING_GUIDE.md` §12 is now "Phases 5–11"; §12.11 = 2FA, §12.12 =
the test harness, §12.13 = Still ahead.

## Still ahead (§12.13)
- **Sub-agents (Level-2):** `agent.spawn(name, task)` running a nested
  `AgentRuntime` so a child's intermediate calls don't pollute the parent context;
  hard part is **budget composition** (parent ceiling carved into sub-budgets). The
  `allowed`-tools plumbing is already there for per-agent allowlists.
- **Larger-corpus RAG:** swap the brute-force numpy cosine store for Qdrant/HNSW
  behind the unchanged `rag.*` interface, once a collection crosses ~tens of
  thousands of chunks or hybrid/metadata-filtered search is wanted.

## How to continue
I'll usually upload the current codebase tarball. Work against `/srv/orchestrator/`
conventions above; verify changes with `test.run` (or the in-process ASGI/mock
pattern); keep new tools as `Tool` subclasses under `tools/<ns>/`; document
substantial additions in `LEARNING_GUIDE.md` §12 and update `prompts/orchestrator.md`
if a new tool is added.
