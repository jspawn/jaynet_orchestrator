# JayNet Orchestrator — Handoff (2026-06-18)

This documents everything changed in this working session and how to deploy it. The
accompanying `orchestrator-full.tar.gz` contains the complete project tree **except**
`data/` (your live `chats.db` / `users.db` / `session.secret` / `trace.db` / uploads /
projects are deliberately excluded so unpacking can't clobber them), venvs, and caches.

---

## TL;DR deploy

1. Unpack `orchestrator-full.tar.gz` over `/srv/orchestrator` (it extracts as `orchestrator/…`;
   `data/` is untouched).
2. Sanity-check `config/runtime.yaml` against your live one if it had diverged mid-session
   (see "Config blocks" below — the tarball already contains the merged version).
3. Install the two new systemd user units, reload, (optionally) start RAG.
4. **On the nginx host**: add `proxy_buffering off;` to `location /` and reload. *(Required —
   this was the cause of the static-file truncation.)*
5. `systemctl --user restart orchestrator-web` and hard-refresh the browser (Ctrl-Shift-R).

---

## Changes by area

### Agent runtime / behaviour
- **Iteration budget** raised 10 → 20 (`config/runtime.yaml` `budgets.max_iterations`).
- **`fs` errors name the allowed root** instead of a bare "outside allowed_roots", so the
  model stops brute-forcing directories (`tools/fs/ops.py`).
- **`rag.search` fails soft**: it checks the collection *before* embedding, so a missing/empty
  collection returns an empty result naming the available collections (no embedder call, no
  400). If the collection exists but the embedder is down, the error is actionable and points
  at `serve.start(kind="embedding", wire_rag=true)` (`tools/rag/store.py`).
- **Prompt** (`prompts/orchestrator.md`): the conversation itself is the record of the session
  (don't search `rag`/`memory`/`fs` for prior turns); a "Files and where things live" section
  naming the writable root; an anti-flailing note (don't re-fetch empty pages, don't guess
  hostnames — search first).
- **web-research skill**: heuristic for JS-heavy/geoportal pages → pivot to the data API
  (WFS/WMS/OGC/OGD/REST), `GetCapabilities` first, read the server's error text.
- **arxiv fix**: switched to `https://export.arxiv.org` and `follow_redirects=True` (the API
  301-redirects http→https; httpx wasn't following it). Endpoint is overridable via
  `tools.arxiv.api_url` (`tools/arxiv/search.py`).

### Thinking toggle (new)
A per-run "thinking" switch threads `think` from the UI → `/api/chat` → `run()` → both model
turns, which send `chat_template_kwargs:{enable_thinking:…}`. The tools template injects an
empty `<think></think>` when off so Qwen3 skips chain-of-thought; when on/absent the render is
unchanged. Files: `runtime/loop.py`, `config/qwen3-tools.jinja`, `web/server.py`, UI.
*Caveat:* depends on LiteLLM forwarding `chat_template_kwargs` and llama.cpp honoring it. If a
build/proxy drops it, the fallback is the Qwen3 soft switch (`/no_think` appended to the
message) — not currently wired, ask if needed.

### Cloud-call approval (new)
`llm.call` (anything in `privacy.remote_llm_tools`) now goes through the same confirmation gate
as `fs.write`/`git.commit` — a cloud call pauses for approval. Controlled by the new
`confirmation` config block (`confirm_cloud_calls: true`). `auto_confirm` (the UI's auto-approve
toggle) bypasses it like any other confirmation. *Scope:* this gates `llm.call` specifically; a
sub-agent spawned onto a cloud model alias is not gated (consistent with the existing privacy
design). Files: `runtime/loop.py`, `config/runtime.yaml`, `prompts/orchestrator.md`.

### RAG on CPU (new)
Two systemd **user** services run the embedder + reranker on the 7950X (CPU-only, both GPUs left
free for the brain and on-the-fly models):
- `systemd/rag-embedding.service` → `Qwen3-Embedding-8B.Q8_0` on `127.0.0.1:8095`
  (`--embeddings --pooling last --n-gpu-layers 0`).
- `systemd/rag-reranker.service` → `Qwen3-Reranker-0.6B.Q8_0` on `127.0.0.1:8096`
  (`--reranking --embeddings --pooling rank --n-gpu-layers 0`).

CPU is forced with `--n-gpu-layers 0` plus empty `HIP_VISIBLE_DEVICES`/`ROCR_VISIBLE_DEVICES`.
`config/runtime.yaml` `tools.rag` points at them directly (not via LiteLLM). zsh aliases
`ragstart` / `ragstop` / `raglogs` are documented in `README.md`.

Install:
```
cp systemd/rag-embedding.service systemd/rag-reranker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rag-embedding.service rag-reranker.service   # or: ragstart
```
With no documents indexed yet, neither needs to run — `rag.search` returns "nothing indexed".
**Open item:** verify the reranker GGUF isn't a broken conversion: rerank one relevant + one
irrelevant doc via `/v1/rerank`; working ≈ 0.98 vs 0.00, broken ≈ e-23.

### Web UI
- **Un-bloated**: the single `index.html` is split into `index.html` + `web/static/app.css` +
  `web/static/app.js`, served via the existing `/static` mount. No behaviour change.
- **Modernized styling**: design-token system (elevation ladder, a consistent radius scale,
  softer borders, focus rings, smooth transitions, thin scrollbars). Single blue accent
  identity kept; change `--accent`/`--accent2` to retheme.
- **Run options moved into the Tools panel** footer: share-private, auto-approve, thinking, plus
  a **per-run Budget** (max iterations / wall-clock / max cost) sent as `budget_overrides`;
  blank field = server default. The old options box was removed and an alpha disclaimer added at
  the bottom of the chat column.
- **Header reordered** to: ＋ new · ☆ save · ⏹ cancel · 🛠 tools · ⚙ admin · 2FA · username ·
  logout (first five are icon-only). New-chat is reachable from the header without opening the
  sidebar.
- **Hero background** (from jaynet.ch): a canvas particle-network + the hashed JayNet mark fills
  the chat area on an empty chat and dims to the back (`opacity .16`) once a prompt is launched.
  Adapted: scoped (no globals), sized to its container via `ResizeObserver`, mouse mapped to
  canvas space, paused when the tab is hidden, reduced-motion shows a static frame. Themed to the
  dark UI; cursor highlight uses `--net-hot` (the accent) — set it to `#e74c3c` for the original
  red. Driven by a `MutationObserver` on `#log` toggling `body.chat-active`.

### CodeMirror bundling + fix
- The 18 separate CodeMirror files are concatenated into **`cm-bundle.js`** (core → `simple.js`
  addon → 15 modes, in order) and **`cm.css`** — the page now makes 2 requests instead of 18.
- **`addon/mode/simple.js`** is now vendored and bundled: the `dockerfile` *and* `rust` modes are
  built on `CodeMirror.defineSimpleMode`, which lives in that addon — it was never vendored, so
  both modes threw "defineSimpleMode is not a function". Fixed.
- The 18 originals under `vendor/codemirror/{lib,theme,mode}` are now redundant; safe to delete
  once confirmed, keeping `cm.css`, `cm-bundle.js`, and `addon/mode/simple.js`.

### Self-test skill (new)
`skills/selftest/SKILL.md` — calls every tool once with the smallest safe input (create-then-
clean-up, throwaway folder, never touches real data) and reports a table. Safe mode by default;
full mode also exercises cloud/serve/job. Trigger by asking "test all the tools" or
`skill.load("selftest")`. It already caught the arxiv bug.

---

## nginx fix (on the nginx host — required)

Symptom was `ERR_CONTENT_LENGTH_MISMATCH` on `cm-bundle.js`: nginx delivered ~130 KB of the
671 KB file and cut the connection. Cause: `location /` proxies to uvicorn **with buffering on**,
and the buffer ceiling (~128 KB, likely `proxy_max_temp_file_size 0;` in `nginx.conf`) truncates
any proxied response over that. Only the SSE block had `proxy_buffering off`.

Fix in the `orch.jaynet.ch` server block:
```nginx
    location / {
        proxy_pass http://192.168.124.102:8071;
        proxy_buffering off;            # <-- add this
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
```
`sudo nginx -t && sudo systemctl reload nginx`. (nginx is on a separate host, so serving
`/static` from disk via `alias` isn't an option without syncing files.)

---

## Config blocks (already merged in the tarball's `config/runtime.yaml`)

```yaml
budgets:
  max_iterations: 20            # was 10

confirmation:
  enabled: true
  non_interactive: allow
  confirm_cloud_calls: true     # cloud llm.call pauses for approval

tools:
  rag:
    db_path: /srv/orchestrator/data/rag.db
    embed_url: http://127.0.0.1:8095/v1/embeddings
    embed_model: embedding
    rerank_url: http://127.0.0.1:8096/v1/rerank
  # tools.serve {...} was added in a prior session (model-server lifecycle)
```

---

## Known caveats / open items
- **Thinking toggle** needs LiteLLM to forward `chat_template_kwargs`; verify by unchecking it and
  confirming the reasoning view stays empty on your uncensored fine-tune.
- **Reranker GGUF** — run the relevant-vs-irrelevant `/v1/rerank` test before trusting it.
- **Model override in the UI** was deliberately not built (not plumbed on the chat request); a
  validated dropdown from LiteLLM aliases + `serve.start`ed models is the clean way if wanted.
- **Static caching**: `app.css`/`app.js`/`cm-bundle.js` are fixed filenames, so hard-refresh after
  edits. A cache-busting `?v=` stamp would need a small `server.py` change.
- **RAG** requires the embedder running for any `rag.*` work; otherwise `rag.search` fails soft.

---

## Models expected in `/srv/models`
- `Qwen3-Embedding-8B.Q8_0.gguf`
- `Qwen3-Reranker-0.6B.Q8_0.gguf`
- (brain + on-the-fly models as before)
