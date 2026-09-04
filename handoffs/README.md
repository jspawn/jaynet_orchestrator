# Handoffs — briefings for AI-assisted modification sessions

Each file here is a self-contained briefing you can paste into a fresh AI
session (or hand to a contributor) to get a specific change done without
re-discovering the codebase first. They describe *where things live*, the
conventions that matter, and how to verify the change — not the whole
architecture (that's [docs/architecture.md](../docs/architecture.md)).

| Handoff | Use it when you want to… |
|---|---|
| [web-ui.md](web-ui.md) | re-theme, restructure or replace the web console |
| [skills.md](skills.md) | teach the agent a new method/domain (a skill) |
| [chains.md](chains.md) | build a fixed multi-step workflow (a chain) |
| [tools.md](tools.md) | add a real tool — Python, declarative connector, or via MCP |
| [plugins.md](plugins.md) | build an optional, toggleable capability bundle (a plugin) |
| [connectors.md](connectors.md) | connect JayNet to an external system (mail, ERP, HTTP API) and share it as .jayconn |
| [chat-templates.md](chat-templates.md) | pick or fix the jinja chat template a local model serves with |

## Ground rules for any session in this repo

- **Tests run from the checkout with the project venv:**
  `.venv/bin/python -m pytest tests/ -q` (full suite, ~1–2 min). Run the subset that
  covers your change first, the full suite before calling it done.
  [docs/testing.md](../docs/testing.md) lists what each test file covers.
- **No build step, no framework.** Python (FastAPI) backend, plain
  HTML/CSS/JS frontend. If you reach for a dependency, check
  `requirements*.txt` first — it's probably already there or deliberately
  absent.
- **Minimal, style-matching changes.** Read the neighboring code and follow
  its conventions (short plain comments, no premature abstraction).
- **The custom layer vs the repo.** Admin-made skills/chains/tools live in
  `$JAYNET_DATA/custom/` — outside the git tree, surviving deploys. Things
  in the repo (`skills/`, `chains/`, `tools/`) are the shipped built-ins.
  Know which layer your change belongs to before you start.
- **Version + changelog.** Version is `runtime/__init__.py` only; notable
  changes get a `CHANGELOG.md` entry under *Unreleased*.
- **Docs are part of the change.** If you change behavior, update the
  matching file in `docs/` — the README links every one of them.
