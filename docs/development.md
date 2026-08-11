# Development

Run the suite from the checkout with its own venv (created by
`scripts/setup.sh` / `scripts/quickstart.sh`, or `uv venv .venv` +
`uv pip install -r requirements-test.txt ...` by hand):

```
cd <checkout> && .venv/bin/python -m pytest tests/ -q
```

(The author's own split: dev checkout `/srv/orch-dev`, live install
`/srv/orchestrator` — never edit live directly; deploy = git pull +
`systemctl --user restart`.)

Conventions: no cross-test imports (copy helpers), monkeypatch instead of
network, comments short and plain. See `docs/testing.md` for the suite
layout (what every test file covers) and `docs/testing-harness.md` for the
`test.run` harness; `ToDos_for_later.md` holds parked ideas.

Modifying JayNet *with* an AI assistant? `handoffs/` has ready-to-paste
briefings per task type (web UI, skills, chains, tools) — they give a fresh
session the key paths, conventions and verification steps up front.

## CLI (`scripts/orch`)

A console driver for local tests — runs the agent loop directly (model
servers + LiteLLM proxy must be up, but not the web service). Uses the
checkout it lives in (`JAYNET_HOME` overrides):

```bash
.venv/bin/python scripts/orch "What's the weather in Zurich?"
.venv/bin/python scripts/orch --max-cost 0.10 --tools web "cheap quick question"
.venv/bin/python scripts/orch --list-tools          # registry dump, no servers needed
.venv/bin/python scripts/orch --trace <run_id>      # replay a run's trace
.venv/bin/python scripts/orch --details "…"         # per-tool call/error/latency tally
```

Other flags: `--max-iterations`, `--max-wall-clock`, `--share-private`,
`--json-output` (full result dict for scripting). For HTTP-level scripting
use the API (`docs/api.md`) instead — the CLI is the same-process shortcut.
`--doctor` doesn't run the agent at all: it validates the install (env
file, paths, ports, proxy, DBs, GPU, linger, units) with fix hints.

Sibling scripts: `scripts/pull-model` (interactive HuggingFace GGUF
downloader), `scripts/setup.sh` / `scripts/quickstart.sh` (installers —
see README quick start / `docs/install.md`).

## Versioning

SemVer. Single source of truth: `runtime/__init__.py` (`__version__`),
surfaced in `GET /api/health` and the admin Status page; releases are git
tags (`v0.9.0`). Current: **0.9.x** — feature-rich and daily-driven, but the
contracts below aren't frozen yet.

**1.0 = "I found most of the quirks by using it."** It means a stranger can
install, run and rely on JayNet:

- **Stable API contract** ✅ — `docs/api.md` defines the native-client
  surface and the change policy (additive-only within a version; breaking =
  minor bump + CHANGELOG), pinned by `tests/test_api_contract.py`.
- **Stable config & data** ✅ — `docs/upgrading.md`: DB schemas auto-migrate
  additively on boot (rollback-safe), the Studio custom layer lives outside
  the git tree, breaking changes land in `CHANGELOG.md`.
- **Installable from scratch** — the README quick start plus
  `docs/install.md` take you from clone to running services without tribal
  knowledge; no hardcoded hostnames/IPs/paths.
- **Repo hygiene** — git history swept for secrets ✅ (2026-08: no keys or
  tokens ever committed; early history holds only harmless personal files),
  license ✅ (MIT).

Until then the 0.9.x line is contract-hardening and polish, not new features.
