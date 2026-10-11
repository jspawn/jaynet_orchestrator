# Decision log

A decision entry records **why** a piece of code is the way it is when the
reason is a story — a live failure, an eval run, an audit finding — rather
than something the code can say itself. Add one when you fix a bug or add a
guard whose motivation would otherwise live in a multi-line comment block:
move the story here, and leave a one-line comment at the code site with the
actionable reason plus the entry id (`# <reason> — D042`). Don't add entries
for self-evident rationale or one-line comments; an entry is for history a
future reader needs to not repeat a mistake. Ids are assigned sequentially
and never reused.

## D001 — Product-free turns don't reset the stall ladder

Context: live run `tb-huarong`.

Tools that never produce a work product on their own — bookkeeping (todo
list, pins, badges) and verify-only exec (`code.check`, no writes by
contract) — do not reset the stall ladder. A hesitant brain can otherwise
hide in planning or in re-running the same check forever: tb-huarong ran 11
todos turns, then 14 `code.check` runs of the same analysis script. Legit
verify loops interleave `fs.edit`/`fs.write`, which DO reset — only
product-free streaks escalate.

Where: `runtime/loop.py` — the `_NO_PRODUCT_TOOLS` frozenset; a turn of ONLY
these tools counts as no-progress in the stall bookkeeping.

## D002 — The slash-command spawn path must pass the cloud gate

Context: audit B9.

The loop's `ctx.spawn` gates a cloud child brain via
`cloud_gate.spawn_gate`; the slash-command spawn path called `runtime.run`
directly and skipped it. A slashed spawn on a cloud alias sends the child's
whole conversation off-box, so `confirm_cloud_calls` applies there too (a
slash run starts fresh — no taint, so only the standard confirmation can
trigger).

Where: `runtime/loop.py` `slash_spawn` — gates the child model through
`cloud_gate.spawn_gate` before running it.

## D003 — Brain_mode=verify removes coding tools instead of advising

Context: recurring eval failures.

When a coding specialist is present, the brain loses the write/run coding
tools and gets `code.check` instead, so implementation MUST route through
`specialist.delegate`. The recurring eval failure was the brain grinding
`code.run` inline and never delegating (gaia-50ad0280: 15 calls,
gaia-65afbc8a: 36, tb-regex-log: 28); prompt tripwires were ignorable, a
missing tool is not.

Where: `runtime/loop.py` — `_BRAIN_GATED_CODE_TOOLS` and
`_brain_code_gate`; the same predicate also bars `tools.load` from re-adding
the gated tools mid-run.

## D004 — The just-reply bounce fires only on tool-less runs

Compute/fresh-data markers in the user message arm the just-reply bounce
(`agent.just_reply_check`): a final answer delivered with ZERO tool calls in
the whole run bounces once. The trigger fires ONLY on tool-less runs, so
keyword overreach costs at most one clarifying turn on knowledge questions
("how many legs has a dog" → "no tool applies" clears it) — while the live
failure cluster it exists for (just-replied counts, decoded strings,
multi-hop answers from memory) is exactly "question + zero tools".

Where: `runtime/loop.py` — `st.just_reply_armed(...)` arming in `run()`;
the bounce itself is a final-answer guard in `runtime/final_guards.py`.

## D005 — Code-side floor for admin_only_tools

Context: claude audit 2026-10-06, tier-1 #1.

A missing or misspelled `security` key must never silently turn
`admin_only_tools` off. The shipped config may EXTEND the code-side set,
never shrink it — these are the tools that reach the host as the service
user or reconfigure the box (`model.measure` hibernates every model for up
to 30 min).

Where: `runtime/loop.py` — `DEFAULT_ADMIN_ONLY_TOOLS` merged under
`security.admin_only_tools` in `_admin_only_patterns`; kept in sync with
`config/runtime.yaml`'s security section.

## D006 — Gate wording rides the tool description at the decision point

Context: live run `tb-regex-log`.

Under `brain_mode: verify/dispatch` the routing rule is appended to the
description the gated brain reads at the DECISION point — a standing prompt
bullet is 30k tokens behind it by the time it picks `fs.write` for an
implementation (tb-regex-log: 6 inline writes past the soft nudge). Dispatch
wording states the hard rejection, not advice.

Where: `runtime/loop.py` — `_BRAIN_GATE_DESC` / `_BRAIN_DISPATCH_DESC`,
applied by `_brain_gate_schema_notes` to copies of the tool schemas.

## D007 — Scratch dir is stable per conversation, keyed, wiped at start

Context: audit #14; audit #23 C1.

With a work_root (web chat/project runs) the scratch dir is a STABLE
per-conversation path, emptied at run START (not end) — the system prompt
quotes this path in its workspace block, and a per-run path there broke the
server prompt cache for the whole replayed chat history (audit #14). The
key scopes the wipe: a chat's runs share `<wr>/.tmp/scratch/<conv-id>`, so
concurrent runs in DIFFERENT chats of the same project no longer delete
each other's temp files (audit #23 C1 — the project files root is shared
across chats), and children (depth>0) always key by their own run_id so a
delegation can't wipe its parent's scratch mid-run. Without a work_root
(CLI) it falls back to an ephemeral per-run TemporaryDirectory, removed on
ANY exit path: explicitly at run end, or via its finalizer if setup raises
before the loop's own try/except takes over (mkdtemp leaked the dir on that
path).

Where: `runtime/loop.py` — `_setup_scratch`; the call site in `run()` has
the short version.

## D008 — Badge-gate state lives in the loop, not the nudge guard

Context: audit #28 C2.

The j-space badge gate's arming (`badge_watch`) and unlock (`badged`)
signals are recorded in the loop's own post-call path — BadgeWatchGuard owns
ONLY the reminder text, so ablating the nudge (`guards_off:
["badge_watch"]`) can no longer silently switch the gate off. The recording
runs before the post-tool guards so the nudge sees the fresh state in the
same pass.

Where: `runtime/loop.py` — the `run.badge` / `skill.load` bookkeeping in
`_record_tool_results`, and `_badge_skill_name` (the ARMING signal lookup).

## D009 — Post-tool guards replay the inline era's side-effect order

Context: audit P2 step 3 (guard extraction).

The per-result rails (failure streak, host give-up, verify-arm bookkeeping,
delegate soft nudge, badge watch) are iterated in their historical
SIDE-EFFECT order — the hint text reassembles in the legacy fail → delegate
→ badge → host order via each guard's slot, so the appended content is
byte-identical to the inline era.

Where: `runtime/loop.py` — the post-tool guard pass in
`_record_tool_results`; slot order defined in `runtime/turn_guards.py`.

## D010 — Per-answer bounce cap and the last-clean fallback

Context: audit item 7; claude audit 2026-10-06 #5.

`agent.max_bounces_per_answer` caps the bounce-nudges ONE final answer may
earn — each bounce costs a full model turn over a growing context, so on a
~30 tok/s brain the historical worst case (8+ bounces) meant minutes of
stall. Counted per answer (a turn with tool calls resets); 0 disables. The
last candidate that passed every answer-shape guard (cap/trunc/empty/markup)
is the fallback a bounce-capped shape guard returns instead of accepting
broken markup (claude audit 2026-10-06 #5); it resets alongside
`answer_bounces` when tool work starts a new answer.

Where: `runtime/loop.py` — the final-answer guard setup in `run()`
(`max_bounces`, `_last_clean_answer`); the chain itself lives in
`runtime/final_guards.py`.

## D011 — Pre-turn guards replay the inline era's firing order

Context: audit P2 step 3 (guard extraction).

The rail-style checks that fire at turn start (budget/context pressure
nudges, stall ladder, deliverable early warning, loop-guard wrap-up
announcement) are iterated in their historical firing order — ORDER IS
LOAD-BEARING. A fired guard returns an action; the loop appends its message
and emits its events so the message/event interleaving stays exactly as the
inline era.

Where: `runtime/loop.py` — the pre-turn guard pass in `run()`'s main loop;
registry in `runtime/turn_guards.py`.

## D012 — Verify-tamper file list must reach the calling brain

Context: j-space-loop live validation 2026-10-05.

The undeclared-edit case (a child run unverified on an instructed test
edit): the changed-file list must reach the CALLER — the delegate tool
returns the error string to the calling brain, which otherwise sees only
the opaque "stuck on the same failure" line.

Where: `runtime/loop.py` — the `verify_state["tampered"]` branch of the
verifier give-up path in `run()` appends the changed files to
`rs.error_msg`; `_finish_run` also surfaces them as `verify_tampered`.

## D013 — Product-free turns count as no-progress

Context: live run `tb-huarong`.

Product-free turns (todo/pin/badge fiddling or verify-only `code.check`
streaks) bump the mutation generation but produce no work product — a brain
can hide in them indefinitely (tb-huarong: 11 todos turns, then 14
`code.check` runs of one analysis script, ladder stuck at rung 1). They
count as no-progress like any read.

Where: `runtime/loop.py` — the `_no_product` computation in the stall
ladder bookkeeping.

## D014 — Byte-identical rewrites are no-change turns

Context: live run on the Taichu brain.

A no-change turn is one where every call was a poll-safe probe or an
identical twin of an earlier success — the mutation bump says "changed",
but byte-identical rewrites change nothing (Taichu brain: 45× the same
`fs.write`).

Where: `runtime/loop.py` — the `_no_change` computation in the stall ladder
bookkeeping (fed by the `repeat_ok` flag set in `_record_tool_results`).

## D015 — Fresh-diagnostic code.check turns are ladder-neutral

Context: delta-fail investigation 2026-10-06, live run `tb-regex-log`.

A `code.check`-ONLY turn whose every call is NEW — args not a repeat of an
earlier call AND the result different from the immediately previous check
result — is diagnostic progress the mutation generation can't see: six of
these found the root cause, then the armed hard stop blocked the write
carrying the fix. NEUTRAL: such a turn neither advances nor resets the
ladder. Identical-repeat check turns still count as no-progress (the "14
identical runs" case the rule was built for), and the FIRST check turn of a
run is never "fresh" — there is no previous result to differ from.

Where: `runtime/loop.py` — the `_fresh_diag` computation in the stall
ladder bookkeeping.

## D016 — Fresh-research turns are ladder-neutral

Context: ablation trace-dig 2026-10-08.

A turn of ONLY web/arxiv/browser calls — all ok, none an identical-args
repeat — is research progress the mutation generation can't see either:
reading fresh pages IS the work on research tasks. The ladder otherwise
escalates mechanically through read-only research (rungs 3/5/7, "produce a
deliverable NOW") and derails runs that pass when left alone
(gaia-46719c30 / d0633230 / dc22a632 — verify_arm was a telemetry
bystander there, it injects no text). NEUTRAL, same as fresh diagnostics.
Identical repeats (`repeat_ok`) and failed fetches still count as
no-progress; near-dup rewordings keep their own guard's nudge.

Where: `runtime/loop.py` — the `_fresh_research` computation in the stall
ladder bookkeeping.

## D017 — A backend blip must not erase the run's partial answer

Context: readiness audit BE-3.

The generic exception handler preserves the partial answer like the
budget/stall handlers do — an un-retried backend blip used to REPLACE
everything the run had produced with the raw error (172 lost answers in 14
days, most of them proxy-restart ConnectErrors).

Where: `runtime/loop.py` — the `except Exception` handler in `run()`.

## D018 — Never replay an assistant message with neither content nor tool_calls

Context: readiness audit BE-8.

A reasoning-only turn cut at the token cap comes back `content=None`, and
re-sending it makes llama.cpp/LiteLLM 400 the next request ("Assistant
message must contain either content or tool_calls") — the cap nudge below
then killed the run it was meant to rescue. The empty-content turn is
rewritten to `content: ""` before appending to the transcript.

Where: `runtime/loop.py` — `_run_model_turn`.

## D019 — Plugin run-end hook fires on every terminal path, off the event loop

Context: audit #26 C1.

The `on_run_end` hook is per-run cleanup that must not depend on the model
remembering (the h5i plugin closes the run's browser session there — the
brain almost never calls close, and live h5i sessions pile up). It fires on
EVERY terminal path (ok/error/cancelled/budget) from the shared finish
block. `fire()` is SYNC and handlers run subprocesses (h5i browser close),
so it goes through `to_thread`: a 10s close on the loop thread would stall
every other run's token stream in this one-process service.

Where: `runtime/loop.py` — `_finish_run`.

## D020 — The strength gate exists because the nudge alone doesn't move a small MoE

Context: live evidence, run #3.

The enforce-mode companion to the routing nudge: run #3 had 5/5 security
cases stay on the default brain (one outright refusal) — the nudge alone
doesn't move a small MoE. When the request matches strength keywords for a
tag with a live OR swappable route (`strength_route` plan), inline
implementation tools are REJECTED until the first `specialist.delegate`
call (which disarms both gates and performs the swap if needed). Never
fires without a route — same rule as the delegate gate.

Where: `runtime/loop.py` — `_arm_delegate_gates`.

## D021 — Explicit accuracy demands seed a verification [must]

Context: council-vote eval.

An explicit accuracy demand in the user message ("this needs to be exact",
"don't guess") seeds a verification [must] requirement the same way /goal
seeds its criterion — the requirements bounce then forces a verification
pass before the final answer instead of a single-sample guess (in the
council-vote eval the brain answered a counting question with one
`code.check` in 65s).

Where: `runtime/loop.py` — `_seed_requirements`.

## D022 — Guard ablation builds the registry complete and skips the check

Context: guard ablation audit 2026-09-23; audit #23 B1; audit #28 C2.

Eval benchmark variants pass `guards_off` to drop named rails; default None
= every guard active. The final registry is built COMPLETE and ablation
skips only the check: the `pin_answer` hook rides the registry position
(legacy pin point between requirements and deliverable), so building minus
ablated names would lose the run's accepted answer with the pin guard
ablated — the ablation column would grade "" (audit #23 B1). Dispatch-gate
ablation (audit #28 C2): the pre-exec gates emit `guard_fired` telemetry
under their names, so `guards_off` honors them too.

Where: `runtime/loop.py` — `_build_guards`; the `guards_off` parameter on
`run()` feeds it.

## D023 — Every depth-0 run logs a route_decision event

Context: audit #23 follow-up; the 09-24 jev experiment.

The jev routing experiment ran blind — the hook returned a tag or None with
no record anywhere. Every depth-0 run now logs source (plugin / keyword /
none), the tag, the hook's confidence and latency, and what the keyword
router WOULD have picked — the agreement question is answerable from
trace.db alone.

Where: `runtime/loop.py` — the `route_decision` emit in
`_apply_brain_nudges`; the meta dict is built in `_routing_nudge`.

## D024 — The state.md size bound applies to the read, not just the injection

Context: audit #28 D8.

`fs.write` has no size cap and supports append, so state.md can grow
unboundedly, and the old whole-file read ran on the event loop every turn.
Now: `stat()` first; over the cap, seek and read only the newest ~cap
bytes, and the caller runs the read through `asyncio.to_thread`.

Where: `runtime/loop.py` — `_read_state_file`; the per-turn call site in
`_turn_anchor`.

## D025 — The near-duplicate guard compares calls with the same flag signature

Context: live run `house-search` child, turn 5.

Boolean flags switch the execution lane — a `js=true` fetch is the
headless-browser call, a materially different call from the plain GET — so
the near-duplicate guard only compares calls with the SAME flag signature.
Without this the guard ate exactly the `js=true` retry the 403 error hint
advises.

Where: `runtime/loop.py` — `_flag_sig` (used with `_arg_tokens` by the
near-duplicate guard).

## D026 — Stall hard-stop escape hatches: bookkeeping and real writes pass

Context: live run `bonsai code-refactor`; delta-fail investigation
2026-10-06, `tb-regex-log`.

Once the final stall-ladder rung has armed the hard stop, the pre-exec
dispatch gate refuses every tool call EXCEPT the escape hatches — delegate
the remaining work or ask the user; the pure-answer path needs no entry
(giving the final answer is stopping tool calls, not making one).
Bookkeeping (todos/pin/badge) also passes: refusing it doesn't stop a spin
— the model just retries and burns iterations (bonsai code-refactor: 8
refused todos retries after the work was done blew the eval iteration cap)
— and it can never disarm the stop or mask a stall, because it is in
`_NO_PRODUCT_TOOLS`. `fs.write`/`fs.edit` also pass: six fresh diagnostic
`code.check` turns armed the stop, then the write carrying the DIAGNOSED
fix was refused and wrap_up forced an answer stating a fix it couldn't
apply. A real write disarms via the ladder's own mutation reset;
byte-identical rewrites still don't. `code.check` stays blocked on purpose:
an armed stop exists to close verify-spin loops, not to feed them.

Where: `runtime/dispatch_guards.py` — `_BOOKKEEPING_TOOLS` and
`_STALL_HARD_STOP_OK`.

## D027 — The stall ladder switches to wrap-up wording after a verified delegation

Context: live eval run `flash-coder code-bugfix`.

Once a delegation has come back with the deterministic `verified=True` flag
(`rs.delegate_verified`), the generic rung texts ("produce a deliverable
NOW", "delegate it") are wrong advice for a run whose work is already done
and checked — they push the brain into MORE fiddling (flash-coder
code-bugfix: 7 todos turns + a blocked `note.set` after a green delegation
burned the eval iteration cap; the fix was complete and verified).

Where: `runtime/turn_guards.py` — `_STALL_WRAPUP`, the wrap-up variant of
the ladder rung texts.

## D028 — Write-like detection covers procedure.save and shell exec lanes

Context: audit 2026-10-10; live runs K2 + Ornith reps and `tb-regex-log`.

The delegate gate watches inline file-writing tools: a brain racking these
up while a coder specialist sits unused is doing the specialist's job.
`procedure.save` authors .py/.sh straight into the workspace (no `fs.*`
path args), so it counts as write-like by NAME — save+run was a
dispatch-gate bypass (the brain could author and run its own code). Shell
exec tools can write files too: since the coding surface converged on
`code.run`, brains implement via `cat > f <<EOF` / `sed -i` and the gate
saw nothing (K2 + Ornith reps: 0 delegations, gate never tripped).
`code.check` is in the set because under `brain_mode=verify` the brain
keeps it as its only shell lane and WILL implement through it
(tb-regex-log).

Where: `runtime/turn_guards.py` — `_DELEGATE_GATE_TOOLS` /
`_EXEC_GATE_TOOLS` (with `_gate_write_like` and `_SHELL_WRITE_RE`).

## D029 — A final wall-clock notice for runs that research through the checkpoint

Context: live eval run `gaia-dc22a632`.

The 0.8 checkpoint nudge is project-oriented ("save, hand off"), but
question-answering runs kept researching straight through it and died on
the clock with no answer at all (gaia-dc22a632: 36 web calls, no FINAL
ANSWER). One blunt "answer NOW" fires at the wall-clock's last stretch
(`budget.final_warn_fraction`, default 0.95) — after this there is no next
turn to recover in.

Where: `runtime/turn_guards.py` — `BudgetFinalNoticeGuard`.

## D030 — Deliverable early warning, because the final-answer check fires too late

Context: live eval run `tb-count-dataset-tokens`.

The mid-run deliverable reminder exists because the final-answer
deliverable check only fires when the model STOPS — a run that burns its
last iterations still computing never gets to react (tb-count-dataset-tokens:
nudge at the cap, answer.txt never written). With >= 2 iterations remaining
and task-named files missing, the guard reminds once; the final-answer
check stays the backstop.

Where: `runtime/turn_guards.py` — `DeliverableReminderGuard`.
