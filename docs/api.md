# JayNet HTTP API — stable contract (v1.0)

*Integrators — the stable HTTP API contract for external clients.*

The surface native/CLI clients code against. **Stable** means: no field is
removed, renamed, or changes meaning without a minor version bump and a
`CHANGELOG.md` entry. Additive changes (new optional request fields, new
response fields, new SSE event types) may ship in any release — **clients
must ignore unknown fields and event types**.

Everything not listed here (admin routes, pages, studio, projects) is
internal to the web UI and may change without notice. Admin-only routes
worth knowing about for ops: `GET /api/admin/backup` downloads a `.tar.gz`
of the data stores, and `POST /api/admin/restore` (multipart `file`) replaces
them from such an archive (requires a service restart afterwards). Three
more that automation tends to touch: `PUT /api/admin/preset-slots`
(`{"updates": {slot: preset}}`) assigns the boot model slots and re-renders
the proxy config; `GET /api/admin/flags` and `GET /api/admin/flags/{id}`
return the privacy-scrubbed flagged sessions (structure only — message
texts and tool contents are stripped, unless the user opted in to
`include_private` on the flag, which keeps the first message and final
answer capped); `GET /api/admin/evals/run-status`
reports the eval suite in flight (`running`, `current`, `last`,
`cancelling`).

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
{"ok": true, "version": "<__version__>", "tools": <registry size>}
```

(`version` is `runtime.__version__`; `tools` counts the core registry —
121 on a core-only boot. The example shows shapes, not pinned values:
two release cuts shipped this block one version behind — audit #24/#25 D1.)

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
also return `{"run_id"}`. `/selftest` shows help without a model;
`/selftest all|tools|skills|<names…>` starts a scoped agent self-test.

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

(Gated by `voice.enabled` in runtime.yaml — on by default; a disabled
channel answers `404`.)

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

## Protocol v1

The typed, versioned client/run protocol (design: `docs/harness-lessons.md`
§2). Two pieces: a typed event registry (`runtime/protocol.py` — the source
of truth) and a versioned REST surface (`/api/v1/*`). The unversioned paths
above remain supported aliases with identical behavior; `/api/v1` is where
the typed contract is guaranteed.

### Event envelope

Every run event — on `/api/stream/{run_id}` and
`/api/v1/runs/{id}/events` alike — travels in one envelope, built only by
`runtime/protocol.build_envelope`:

```json
{"v": 1, "run_id": "…", "seq": 7, "ts": 1792400000.0,
 "type": "model_turn", "iteration": 3, "data": {…}}
```

`seq` is per-run, monotonically increasing, and float-tolerant (the server
may inject a notice at a fractional seq — treat it as an opaque ordering
token, compare with `>` only). `iteration` is the agent-loop turn the event
belongs to (0 for run-level events). The registry validates every payload at
the emit seam — warn-only in production, strict in tests — so the following
types are exhaustive for `v: 1`:

| group | event types |
|---|---|
| run lifecycle | `run_start`, `run_finish` (terminal; closes the stream) |
| model traffic | `model_start`, `model_turn`, `token` (streamed delta; deliberately NOT in the trace), `cost` |
| tools | `tool_selection`, `tool_result`, `output` (downloadable artifact) |
| sub-agents | `subagent_start`, `subagent_finish`, `subcall`, `auto_delegate` |
| human-in-the-loop | `confirmation`, `confirmation_request`, `questions_request` |
| progress/planning | `progress` (free-form), `todos`, `ledger_check`, `badge`, `compaction` |
| routing/setup | `route_decision` (free-form), `procedure_autoload`, `strength_gate` |
| guard telemetry | `guard_fired` (uniform per-rail) plus the rails' own events: `budget_warning`, `context_warning`, `stall_check`, `stall_hard_stop`, `deliverable_warn`, `repeat_blocked`, `fresh_retry`, `bounce_cap`, `requirements_gate`, `deliverable_check`, `verify_check`, `just_reply_check`, `procedure_check`, `markup_leak`, `empty_final`, `model_turn_capped`, `model_turn_truncated` |
| verify gate | `verify`, `verify_giveup` |

Per-type `data` fields: `runtime/protocol.py` `EVENTS` is the authoritative
spec (required + known-optional fields per type). New event types and new
fields are additive — ignore what you don't know.

### Versioned endpoints

Same auth (session cookie or `Bearer jn_…`), same bodies as the unversioned
aliases.

- `POST /api/v1/runs` — start a plain agent run. Body mirrors `POST
  /api/chat` (`message` required; `history`, `think`, `project_id`,
  `conversation_id`, plus the advanced overrides). Slash commands are a
  `/api/chat` feature — a `message` starting with `/` gets `422`. →
  `{"run_id": "…", "v": 1}`
- `GET /api/v1/runs/{run_id}` — status from the trace store:
  `{"run_id", "v", "status", "started_at", "finished_at", "total_tokens",
  "cost_usd", "owner"}`. `final_answer` is included only when the caller
  owns the run (admins get metadata, not another user's content). Unknown
  or foreign run → `404`.
- `GET /api/v1/runs/{run_id}/events` — SSE, same semantics as
  `/api/stream/{run_id}` (ping ~15 s, closes after `run_finish`). Resume via
  `Last-Event-ID` or the additive `?after_seq=` query parameter.
- `POST /api/v1/runs/{run_id}/cancel` → `{"ok", "cancelled"}`;
  `POST /api/v1/runs/{run_id}/approve` and `POST
  /api/v1/runs/{run_id}/answer` — same request bodies as the unversioned
  routes.
