# Handoff: your own web UI / theme

**Goal:** re-theme, restructure, or completely replace the JayNet web console.

## What you're working with

No framework, no build step — plain HTML/CSS/JS served as static files by
FastAPI. Everything frontend lives in `web/static/`:

| File | What it is |
|---|---|
| `index.html` | the chat console markup (sidebar, chat area, composer, ToDos panel) |
| `app.js` | all chat logic (~2400 lines): SSE streaming, rendering, settings, panels |
| `app.css` | every style, both themes (~1000 lines), organized in labeled sections |
| `admin.html` | the admin console — markup **and inline JS** in one file |
| `account.html`, `login.html` | account menu / login page |
| `dialog.js` | styled `dlgAlert`/`dlgConfirm`/`dlgPrompt` — native dialogs are banned |
| `files.js` | the project file manager |
| `vendor/` | vendored JS libs (licenses: `THIRD_PARTY_NOTICES.md`) — no CDN, ever |

Backend: `web/server.py` + `web/routes_*.py` (FastAPI). The chat stream is
SSE at `GET /api/stream/{run_id}`; event kinds are handled in `app.js`
(search for the `todos`/`model_turn` cases) and the stable HTTP surface is
documented in `docs/api.md`.

## Theming (the 80% case)

- All colors are CSS variables in `:root` at the top of `app.css` (dark is
  the default theme). The light theme is a **pure override layer** at the
  bottom of the file (`body.light …`, toggled per browser via the user menu;
  `app.js` sets the class from localStorage `jaynet.theme`).
- A custom theme = edit the `:root` variables (and the `body.light` block if
  you keep dual themes). Don't introduce hardcoded colors elsewhere — the
  audits check for that.
- Body classes in use: `light`, `todo-collapsed`, nerd/chat-style classes —
  grep `classList` in `app.js` before inventing new ones.

## Conventions that bite if you miss them

- **Cache-busting:** `index.html` loads `app.css?v=N` and `app.js?v=N`.
  Bump `N` when you edit either file, or browsers keep serving the stale
  asset. `admin.html` needs no bump (its JS is inline).
- **Dialogs:** use `dialog.js` (`dlgAlert`/`dlgConfirm`/`dlgPrompt`) — native
  `alert/confirm/prompt` were deliberately removed (browser dialog-blocking
  broke flows). Keep that invariant.
- **XSS discipline:** anything model- or user-produced goes through text
  nodes / inert DOMParser, never raw `innerHTML` with live data (there was a
  paste-jacking audit finding; `tests/test_web_regressions.py` pins several
  of these behaviors).
- **Mobile:** the layout is responsive by design (the ToDos panel collapses
  to a tab, etc.). Check narrow viewports after layout changes.

## A fully custom UI instead

If you want your own frontend (React, mobile app, …), don't fork
`web/static/` — build against the **stable HTTP API** in `docs/api.md`
(token auth, chat endpoints, SSE stream). It's additive-only within a
version and pinned by `tests/test_api_contract.py`, so your client won't
silently break.

## Verify

- `python -m pytest tests/test_web_regressions.py -q` — the UI regression net.
- Then a manual pass: run the service, hard-reload (cache-bust!), exercise
  chat streaming, the admin tabs you touched, both themes, narrow viewport.
