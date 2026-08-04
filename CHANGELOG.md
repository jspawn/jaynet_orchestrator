# Changelog

Breaking changes and release notes. Versions are git tags; the stable API
contract lives in `docs/api.md`, upgrade procedure in `docs/upgrading.md`.

## 0.9.0 — 2026-08-04

First tagged release. Feature-complete daily driver; the 0.9.x line is
contract-hardening toward 1.0 (public open-source release) — see README →
Versioning.

Highlights since development started (squashed):

- Web console: multi-user auth (+TOTP 2FA), per-user chats/projects, quick
  settings, run budgets, inline diffs, light/dark theme
- Agent runtime: local-first routing brain + specialist slots, preset
  catalog with GPU/CPU placement, strengths-aware delegation, ~100 tools,
  skills/chains, Studio (admin-created skills/chains/connectors + .jaypack
  share), wiki, memory + KG, trace mining, verify/council/ops tools
- Voice channel `/api/voice` with `voice:false` chat mode for native
  clients; per-user API tokens; SSE streaming; scheduled runs; flags/coroner
- Admin console: status + hardware, processes, presets, prompt, config,
  tools, users, flags, RAG
- Repo hygiene: MIT license, secrets sweep (clean), paths centralized in
  `runtime/paths.py`, nginx example, stable API contract + upgrade guide
