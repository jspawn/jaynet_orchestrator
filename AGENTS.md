# AGENTS.md — operational map for AI assistants

*Read this before touching anything outside the dev checkout. It exists
because the same few mistakes kept recurring: editing the live install,
writing to the wrong database, or scripts silently falling back to a
fresh `~/jaynet-data` because `JAYNET_DATA` was unset.*

## The two checkouts

- **`/srv/orch-dev`** — the development checkout. All work happens here;
  you may commit **only when the user explicitly says so** (they push).
- **`/srv/jaynet-orchestrator`** — the LIVE install. **Never edit, never
  run tests here.** The user does push/pull and restarts the systemd user
  services themselves. (The old `/srv/orchestrator` path is stale; some
  DB rows still carry it in informational `source_path` fields.)

## Where things live (live box "wolf")

- **Data root `/srv/data`** (= `JAYNET_DATA`, set in
  `~/.config/jaynet.env` — the systemd units source that file; your shell
  does NOT). Stores: `presets.db` (preset catalog, slots, GPU/binary
  registry in `meta`), `trace.db` (runs + events), `chats.db`,
  `users.db`, `eval.db`, `memory.db`, `rag.db`, `research.db`,
  `jaynet.db`, `litellm.yaml` (GENERATED — re-rendered from the DBs by
  `ExecStartPre: python -m runtime.cloud_store render` at every proxy
  start; never hand-edit), plus file dirs (`projects/`, `outputs/`,
  `images/`, `audio/`, `chat-scratch/`, …).
- **Models `/srv/models`** — GGUF zoo, one dir per HF repo.
- **Binaries `/srv/jaynet-bin`** — versioned prefixes
  (`llama.cpp.rocm-bNNNNN/{bin,lib}`) + stable symlinks; the preset DB's
  `meta.binaries` maps names (`rocm`, `cpu`, `whispercpu`) to these.
  Source trees + venvs live in `~/jaynet-build` (see
  `scripts/build_tools.sh`).
- **Config** — live: `/srv/jaynet-orchestrator/config/runtime.yaml`;
  secrets in `~/.config/jaynet.env` (never read/copy/print secrets).

## The `JAYNET_DATA` trap (most common error)

Any script or store constructed without the env file loaded falls back to
`~/jaynet-data` and **creates a parallel empty universe** there. When you
run anything against live stores from a shell, always:

```bash
JAYNET_DATA=/srv/data JAYNET_HOME=/srv/jaynet-orchestrator .venv/bin/python …
```

If `~/jaynet-data` appears as a side effect, delete it and redo with the
env set (this happened 2026-10-20 and confused a proxy render check).

## Reading/writing live stores

- **Read:** `sqlite3 /srv/data/<store>.db "…"` freely (WAL).
- **Write:** use the app's own store classes (`runtime.preset_store.PresetStore`,
  `runtime.cloud_store.CloudStore`, …) from the dev checkout with
  `JAYNET_DATA` set — they validate and migrate. Raw-SQL writes are the
  last resort. The running service hot-applies DB edits; only *process*
  changes (a newly assigned boot slot's server) need a service restart or
  a manual start in Admin → Processes.
- Live services run as systemd **user** units (`jaynet-web`,
  `litellm-proxy`) — the user restarts them; don't.

## Before every commit (the CI battery)

```bash
.venv/bin/python -m pytest tests/ -q -n 2          # full suite
.venv/bin/python -m ruff check runtime web tools scripts tests plugins
.venv/bin/python scripts/check_complexity.py       # ceiling 25, refactor — don't bump
bash scripts/check_mypy.sh                         # line-number-sensitive baseline
.venv/bin/python scripts/gen_rails.py --check
.venv/bin/python scripts/check_prompt_size.py
.venv/bin/python scripts/check_doc_tools.py
shellcheck -S error scripts/*.sh
git diff --check
```

Also: regenerate `docs/catalog.md` (`scripts/gen_catalog.py`) when
tools/skills/chains/slash commands change; bump static cache busters in
`web/static/index.html` / `admin.html` when their JS/CSS changes;
baseline growth (mypy/complexity/prompt) needs a `baseline-bump: …`
marker in the commit message.

## Releases

`scripts/tag_release.py vX.Y.Z` after bumping `runtime/__init__.py`, the
CHANGELOG top entry, `docs/releases/vX.Y.Z.md` and the prose version
markers (README, LEARNING_GUIDE, docs/development.md) — the script
validates all of them at HEAD, CI re-checks the markers.
