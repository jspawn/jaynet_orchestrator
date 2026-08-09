# JayNet HTTP API — stable contract (v0.9)

The surface native/CLI clients code against. **Stable** means: no field is
removed, renamed, or changes meaning without a minor version bump and a
`CHANGELOG.md` entry. Additive changes (new optional request fields, new
response fields, new SSE event types) may ship in any release — **clients
must ignore unknown fields and event types**.

Everything not listed here (admin routes, pages, studio, projects) is
internal to the web UI and may change without notice. Two admin-only routes
worth knowing about for ops: `GET /api/admin/backup` downloads a `.tar.gz`
of the data stores, and `POST /api/admin/restore` (multipart `file`) replaces
them from such an archive (requires a service restart afterwards).

## Auth

All endpoints except `GET /api/health` require a **per-user API token**
(minted in Account → Security → API tokens):

```
Authorization: Bearer jn_…
```

The token acts as that user: budgets, tool toggles, chat/project ownership
apply. `JAYNET_WEB_TOKEN` (server env) is a separate global *admin* bearer for
server automation — never embed it in client apps.

Errors: `401` missing/invalid token · `404` unknown run · `422` invalid body.

## Endpoints

### `GET /api/health` — no auth

```json
{"ok": true, "version": "0.9.0", "tools": 112}
```

### `POST /api/chat` — start an agent run (web-UI style, client-managed history)

```json
{"message": "…",
 "history": [{"role": "user", "content": "…"}, {"role": "assistant", "content": "…"}],
 "think": true, "project_id": null, "conversation_id": null}
→ {"run_id": "…"}
```

`history` is optional and client-managed; omit it for a stateless turn.
Advanced per-run overrides (`tools`, `budget_overrides`, `sampling`,
`compaction`, `parallel_tools`, `sub_budget`, `architect_threshold`,
`share_private`, `auto_confirm`, `attachments`) mirror the web quick
settings — optional, ignore them unless you know you need them.
Messages starting with `/` are slash commands (`/goal`, `/compact`, …) and
also return `{"run_id"}`.

### `GET /api/stream/{run_id}` — SSE feed of a run

One `event:` frame per event; the stream ends after `run_finish`. A `ping`
keepalive arrives every ~15 s. Supports the `Last-Event-ID` header to resume
after the given sequence number.

Events a client should handle:

| event | data highlights |
|---|---|
| `token` | streaming text delta of the reply |
| `run_finish` | final `answer`, `status`, budget stats — then close |
| `confirmation_request` | agent asks approval → answer via `/api/approve` |
| `questions_request` | ask.user questions → answer via `/api/answer` |

Everything else (`run_start`, `model_start`, `model_turn`, `tool_result`,
`cost`, `compaction`, `output`, `subagent_*`, `progress`, …) is optional
activity detail — safe to ignore.

### `POST /api/cancel/{run_id}`

```json
→ {"ok": true, "cancelled": true}     // false if the run already finished
```

Unknown run → `404`.

### `POST /api/approve/{run_id}` · `POST /api/answer/{run_id}`

```json
{"confirmation_id": "…", "approved": true}   → {"ok": true}
{"ask_id": "…", "answers": {"q": "a"}}       → {"ok": true}
```

### `POST /api/voice` — native-client turn, server-managed conversation

```json
{"text": "…", "conversation_id": null, "stream": false, "voice": false}
```

- The server holds the thread: send only the new message, pass the returned
  `conversation_id` to continue. Foreign/unknown ids are ignored and a fresh
  one is minted (IDOR-safe).
- `voice: true` (default): short spoken-style answers, thinking off, tight
  budget. `voice: false` (chat clients): full markdown, thinking on, normal
  budgets. Both use a safe unattended toolset (no confirmation-gated tools,
  no cloud `llm.call`).
- `stream: false` → `{"conversation_id", "run_id", "text", "status"}`
- `stream: true` → `{"conversation_id", "run_id"}`; open `/api/stream`.

### `GET /api/tools`

```json
{"tools": [{"name": "web.search", "namespace": "web", "description": "…",
            "private": false, "requires_confirmation": false,
            "parameters": {"type": "object", "…": "…"}, "enabled": true}]}
```
