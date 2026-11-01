# Durable work items and outbox

*Design and implementation record — phase 1 (scheduled prompts) and phase 2 (observe-only arXiv watches) are implemented.*

Inspired by DeepSeek's [dsh-superagent prototype](https://github.com/deepseek-ai/dsh-superagent-prototype),
adapted to JayNet rather than ported. The useful ideas are durable authorized
work, explicit execution attempts, idempotent delivery, and quiet supervision.

## Scope and decisions

Build the shared execution kernel **through scheduled prompts first**. Preserve
schedule definitions, recurrence rules, permissions, run budgets, projects,
and the existing admin Schedules surface. Leave goals, eval schedules, detached
jobs, normal chat, connectors, and tool execution unchanged in phase 1.

Phase 1 deliberately provides:

- one durable work item per scheduled occurrence;
- one or more recorded execution attempts (manual retry comes later);
- atomic queued-attempt claims and explicit startup reconciliation;
- a narrow outbox for scheduled-result chat delivery;
- existing schedule history enriched with occurrence, attempt, and delivery state.

Phase 1 deliberately does **not** provide:

- automatic replay of an agent run that had already launched;
- generic retry/outbox wrapping of tools, connectors, HTTP, MCP, or git;
- inbound triggers, proactive `observe → auto` policies, or a new admin tab;
- migration of `/goal`, `/loop`, eval schedules, jobs, or `/api/v1/runs`;
- a broker, distributed workers, durable SSE, or an exactly-once claim.

Agent execution is **at least once after a crash is ambiguous**. Safety comes
from conservative recovery and idempotent effects, not a false exactly-once
promise.

## Current failure windows

The scheduler currently advances `schedules.json` before launching the run
(`web/scheduler_runtime.py`). A crash afterwards consumes the occurrence without a
run or history. Completion then writes `schedule_runs`, JSON `last_status`, and
the scheduled chat separately, so a crash can lose or duplicate delivery.
Concurrent scheduled chat completion also reads and replaces the whole chat,
which can discard another completion's turn.

The JSON schedule definition and SQLite work record cannot share a transaction.
The design therefore uses a unique occurrence key and tolerates either order of
crash recovery.

## Target flow

```text
ScheduleStore (existing recurrence definitions)
  │ due snapshot: schedule id + next_fire
  ▼
WorkStore.enqueue_occurrence()        SQLite transaction, unique occurrence
  │                                   work item + queued attempt
  ├─ success/already enqueued → ScheduleStore.mark_fired(expected occurrence)
  └─ already active for schedule → do not advance recurrence
  ▼
attempt dispatcher
  │ atomic claim → launched_at immediately before runtime.run
  ▼
existing governed scheduled executor
  │ runtime.run with current role/tool/budget/project rules
  ▼
completion transaction
  ├─ finish attempt + item → delivering
  ├─ insert schedule_runs compatibility history
  └─ enqueue scheduled-chat effect (dedupe key = run_id)
  ▼
outbox dispatcher
  │ ChatStore.append_scheduled_turn() — atomic + run_id idempotent
  └─ mark effect applied + item terminal
```

A disabled/deleted schedule cancels its queued items through the existing admin
and tool paths. It does not kill a run already in flight, matching current
behavior. Ownership, schedule permission, enabled state, and global tool gates
are revalidated before launch; a stored item never grants authority by itself.

## Storage: same `chats.db`

Add `runtime/work_store.py`, constructed with the same configured path as
`ScheduleRunStore`. Keeping work, compatibility history, and chat turns in one
database gives real transactions and existing backup/restore coverage.

### `work_items`

| Field | Meaning |
|---|---|
| `id` | opaque item id |
| `kind` | `scheduled_prompt` in phase 1 |
| `owner`, `project_id` | execution and cleanup scope |
| `source_id` | schedule id |
| `occurrence_key` | unique `schedule:<sid>:<scheduled next_fire>` |
| `payload_json` | immutable prompt/schedule snapshot for this occurrence |
| `status` | `queued`, `running`, `delivering`, `completed`, `failed`, `interrupted`, `cancelled` |
| `created_at`, `updated_at` | audit timestamps |

One active item per schedule prevents overlapping occurrences. Terminal item
retention follows the existing 50-history-rows-per-schedule policy; never prune
queued/running/delivering/interrupted work.

### `work_attempts`

| Field | Meaning |
|---|---|
| `id`, `item_id`, `attempt_no` | attempt identity; unique item + number |
| `run_id` | ordinary trace/run identity, unique when present |
| `status` | `queued`, `running`, `succeeded`, `failed`, `interrupted`, `cancelled` |
| `scheduled_for` | the actual occurrence time, not completion time |
| `claimed_at`, `launched_at`, `finished_at` | distinguishes safe pre-launch recovery from ambiguous launched work |
| `duration_s`, `result_status`, `excerpt`, `tokens`, `error` | existing history fields plus explicit outcome |

`claim_next()` uses `BEGIN IMMEDIATE` to select and mark one queued attempt.
The single web process remains the deployment target; SQLite claims also avoid
in-process duplicate dispatch.

### `work_effects`

| Field | Meaning |
|---|---|
| `id`, `item_id`, `attempt_id` | owning work |
| `kind` | `scheduled_chat_append` only in phase 1 |
| `dedupe_key` | unique `scheduled-chat:<run_id>` |
| `payload_json` | owner, prompt, answer, run id, status |
| `status` | `pending`, `applied`, `failed` |
| `attempt_count`, `next_attempt_at`, `last_error` | bounded backoff diagnostics |
| `created_at`, `applied_at` | delivery audit |

Do not persist arbitrary tool arguments or credentials here. External effects
need adapter-specific payload validation and reconciliation in a later phase.

## Recovery semantics

Run `WorkStore.recover()` before starting attempt/outbox dispatch:

- **Queued attempt:** remains queued and launches normally.
- **Claimed, `launched_at IS NULL`:** return to queued; the agent never started.
- **Launched/running attempt:** mark attempt and item `interrupted`; do not
  automatically replay possibly side-effecting work.
- **Pending chat effect:** retry with backoff. Its idempotent sink makes a
  crash between apply and acknowledge safe.
- **Delivering item with all effects applied:** finish the terminal transition.

An interrupted item is inspectable in schedule history. A later manual retry
must create a new attempt and include a recovery note: inspect the workspace,
trace, and external state before repeating any side effect. Automatic retry of
transient model/network errors is a separate, explicitly bounded decision.

## Idempotent scheduled chat

Add one transaction to `ChatStore`, e.g.
`append_scheduled_turn(owner, prompt, turn)`:

1. find or create the owner's `⏰ Scheduled runs` chat;
2. return successfully if `run_id` already exists in that chat;
3. append at `MAX(idx)+1` without deleting other turns;
4. update the chat timestamp in the same transaction.

This removes scheduled-completion read/replace races. Ordinary client-side
whole-chat saves keep their existing semantics and are not redesigned here.

## Compatibility and admin UI

Keep `ScheduleRunStore` and `schedule_runs` as the history projection. Write its
row in the completion transaction and enrich it from work tables by `run_id`.
Old rows remain readable with no work metadata.

Extend `GET /api/admin/schedules/{sid}/history` fields with item/attempt ids,
scheduled occurrence, delivery status, and interruption/error details. Show
these in the existing history expansion in **Admin → Schedules**; do not add a
Jobs/Work tab. Keep the 50-row cap, owner deletion cleanup, schedule CRUD, and
permission behavior.

`last_status` in `schedules.json` becomes a convenience cache only; work/history
tables are the execution truth. Existing trace rows remain the detailed run
audit, not a queue.

## Phase 1 implementation record

1. **Store kernel:** add `runtime/work_store.py`, schema/migrations, occurrence
   enqueue, active-schedule guard, atomic claim, completion, outbox, recovery,
   pruning, and owner cleanup. Unit-test every transition and crash boundary.
2. **Idempotent delivery:** add `ChatStore.append_scheduled_turn`; test duplicate
   delivery and two concurrent completions without lost turns.
3. **Scheduler integration:** adapt the current tick/fire seam in
   `web/scheduler_runtime.py`; preserve `_DenyConfirm`, role/tool gates, project and
   scratch roots, bus/task ownership, history, and recurrence behavior. Make
   `mark_fired` conditional on the expected occurrence and enabled definition.
4. **Startup and lifecycle:** register recovery and dispatch hooks, cancel queued
   items on disable/delete, clean owner rows, and verify backup/restore of the
   new tables.
5. **History/UI/docs:** enrich existing history and Schedules UI, update
   `docs/admin.md`, `docs/architecture.md`, `docs/code-map.md`, and this brief's
   status. Bump the admin static cache version when JS changes.
6. **Verification:** focused scheduler/store/chat tests, then the complete CI
   battery in `AGENTS.md`. Fault-inject after enqueue, after recurrence advance,
   before/after launch, after model completion, and between chat apply/effect
   acknowledgement.

## Phase 2 implementation record: observe-only arXiv watches

An admin can attach `source: arxiv` and a query to a recurring schedule. The
server fixes `mode: observe`, rule/prompt versions, read-only tool scope, and a
budget snapshot; the client cannot supply them. The existing schedule supplies
owner, project, future prompt, recurrence, and enabled state.

`web/trigger_runtime.py` revalidates owner, project, schedule permission,
source, interval, rule/prompt versions, tool availability/global disable state,
and current budget ceilings before fetching and again before recording. Polls
use only the registered read-only `arxiv.search` tool (20 results, last-updated
order). A pause, deletion, or mandate change during the fetch records nothing.

`runtime/trigger_store.py` adds `trigger_events` and `trigger_decisions` to
`chats.db`. A unique `(source_id, external_id)` dedupes versioned paper IDs.
The first successful non-empty poll records `baseline`; later unseen versions
record `would_accept`. Each immutable decision carries its reason, versions,
occurrence, and mandate hash. History is capped at 50 API rows, and owner
cleanup cascades decisions. Source errors advance recurrence to avoid hot-looping.

There is deliberately **no code path from a decision to `WorkStore`**.
Observation never invokes `runtime.run`, creates work/attempts/effects, executes
the future prompt or tool scope, or appends chat. The existing Admin → Schedules
history surface displays decisions; no new tab was added.

## Later phases

- **Phase 3 — selected external effects:** adapter-specific idempotency and
  reconciliation for named connector/report destinations. Never globally wrap
  `AgentRuntime._execute_tool()` and blindly replay arbitrary side effects.
- **Phase 4 — shared supervision:** evaluate migrating goals and other durable
  workflows; add human-priority scheduling only when a shared queue exists.
  Any future auto mode must link work to an immutable decision and revalidate
  its mandate before enqueue and launch; source text remains untrusted data.
