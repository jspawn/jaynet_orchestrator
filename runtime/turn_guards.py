"""Pre-turn and post-tool guards (code audit 2026-09-23, P2 step 3).

The rail-style checks that historically sat inline at two points of
AgentRuntime.run are now registered classes, mirroring the final-answer
guards (runtime/final_guards.py, step 2):

- PRE_TURN_GUARDS fire at turn start (after the compaction pass, before
  the model call): one-shot budget/context pressure nudges, the stall
  ladder, the deliverable early warning and the loop-guard wrap-up
  announcement. A guard's check() returns a PreTurnAction (message +
  events) or None; the LOOP appends the message and emits the events, so
  the message/event interleaving stays exactly as the inline era.
- POST_TOOL_GUARDS fire after each tool result is recorded (before the
  tool message is appended): the failure-streak nudge, per-host give-up,
  verify-arm bookkeeping, the delegate soft nudge and the badge watch.
  A guard's check() returns None when it did not fire, else its hint
  text (possibly "" — the verify-arm guard's fire IS the arming, it
  appends no text). The loop reassembles the hints in the LEGACY
  concatenation order fail → delegate → badge → host
  (_POST_TOOL_HINT_SLOTS) — registry order encodes the historical
  SIDE-EFFECT order, the slot order the byte-identical result content.

ORDER IS LOAD-BEARING in both registries — it is the historical order of
the inline blocks, kept exactly so event ordering, hint ordering and the
stuck-signal sequence in traces are unchanged. Rationale comments (audit
references, live-case notes) moved here with their checks.

Telemetry (audit P2 step 4): the loop emits one uniform `guard_fired`
event — {"name", "phase": "pre_turn"|"post_tool", "turn"} — for every
guard application, IN ADDITION to the guard's legacy events/hints (those
stay byte-identical), giving each rail a fire rate in trace analysis.

Guard config: each guard reads its own keys from the agent:/loop_guard:
sections carried by TurnGuardContext; values shared with inline code
(the stall bookkeeping, the pre-exec delegate gates, the pre-exec
fresh-retry rewrite) are parsed once in loop.py and passed in.

Deliberately NOT here (still inline in loop.py):
- the compaction pass — transcript hygiene on its own cadence, not a rail
- the pre-exec dispatch gates (malformed/admin/allowlist/stall-hard-stop/
  strength/dispatch/delegate-enforce/JSON/duplicate/near-dup/privacy/
  confirmation) — per-call plan rejections with continue semantics, not
  nudges (the hard-stop's ARMING lives in StallLadderGuard; only the
  per-call refusal is inline)
- the wrap-up turn's tool-call cut (break semantics)
- the verify_spec verifier gate — a bounded retry loop with its own stall
  breaker and break semantics, not a one-shot nudge (see final_guards.py)
- the stall-ladder bookkeeping (mutation-gen reset/no-progress counting)
  — state update, no nudge
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .run_state import RunState

# Stall ladder: the frozen-brain pattern (live eval: read a few files, then
# stop without ever producing anything) is a run of consecutive turns with no
# mutation — only reads/searches/polls. Each rung fires ONCE per run, every
# `after` no-progress turns, escalating from "act now" to "produce or ask".
# Text is injected as a system message before the next model turn.
_STALL_RUNGS = [
    ("Progress check: the last {n} turns only inspected or queried — nothing "
     "was created or changed. Say your next concrete step in one sentence, "
     "then DO it now: write a first rough version, run a small experiment, "
     "or delegate it. A rough attempt you can improve beats more inspection."),
    ("You still have not produced anything. If you are unsure how to solve "
     "this: write the dumbest working version first and run it — a concrete "
     "error is easier to fix than a blank page. Missing knowledge? Search or "
     "read the docs. Missing information only the user has? Ask.{delegate}"),
    ("Final progress warning: several turns without any concrete output. "
     "Produce a deliverable NOW with the best approach you have — imperfect "
     "is fine — or tell the user plainly what blocks you and ask for a hint. "
     "Do not continue inspecting.{delegate}"),
]


def _budget_warning(pressure: float, dim: str, elapsed_s: float = 0) -> str:
    """The checkpoint nudge injected once the run nears a ceiling."""
    elapsed = ""
    if elapsed_s > 0:
        m, s = divmod(int(elapsed_s), 60)
        elapsed = f" (running for {m}m {s}s)" if m else f" (running for {s}s)"
    return (
        f"⚠ BUDGET NOTICE: this run has used about {int(pressure * 100)}% of its "
        f"{dim} budget{elapsed} and will be cut off when it hits the limit. Do NOT start new work "
        f"or spawn new sub-tasks. Land the plane now:\n"
        f"1. Finish the current step only if it's nearly done.\n"
        f"2. Save in-progress work to the project (fs.write / deliver.files) so nothing is lost.\n"
        f"3. Write or update NEXT_STEPS.md in the project: what's done, what remains, and how "
        f"to resume in a fresh run.\n"
        f"4. Give the user a short summary of where things stand, then stop.\n"
        f"A clean hand-off beats squeezing in one more change."
    )


def _context_warning(pressure: float, ctx_tokens: int) -> str:
    """The checkpoint nudge injected once the prompt nears the model's context
    window. Without it, the first symptom of a full window is the server
    rejecting the turn (HTTP 400) and the run dying as an internal error."""
    return (
        f"⚠ CONTEXT NOTICE: this run's prompt has grown to about {int(pressure * 100)}% of the "
        f"model's {ctx_tokens:,}-token context window. When it fills, the run ends abruptly. "
        "Change gear now:\n"
        "1. Do NOT re-read large files or long outputs — work from what is already in context.\n"
        "2. Save in-progress work to the project (fs.write / deliver.files) so nothing is lost.\n"
        "3. Write or update NEXT_STEPS.md in the project: what's done, what remains, and how "
        "to resume in a fresh run.\n"
        "4. Give the user a short summary of where things stand, then stop.\n"
        "A clean hand-off beats filling the window mid-edit."
    )


# The delegation verb under both names: specialist.delegate is canonical;
# code.delegate is the hidden legacy alias (tools/code/delegate.py). Either
# one arms/disarms the delegate and strength gates and feeds the fresh-retry
# bookkeeping below.
_DELEGATE_TOOLS = frozenset({"specialist.delegate", "code.delegate"})

# Check/execution tools — a call to one of these AFTER a delegation counts
# as verifying the specialist's report (agent.verify_delegate_check).
_CHECK_TOOLS = frozenset({"code.check", "code.run", "code.execute"})

# Inline file-writing tools the delegate gate watches: a brain racking these
# up while a coder specialist sits unused is doing the specialist's job.
_DELEGATE_GATE_TOOLS = frozenset({"fs.write", "fs.edit", "code.patch"})

# Shell exec tools can write files too — since the coding surface converged
# on code.run, brains implement via `cat > f <<EOF` / `sed -i` and the gate
# saw nothing (live: K2 + Ornith reps, 0 delegations, gate never tripped).
# code.check is in the set: under brain_mode=verify the brain keeps it as its
# only shell lane and WILL implement through it (live: tb-regex-log).
_EXEC_GATE_TOOLS = frozenset({"code.run", "code.execute", "code.check"})

# A shell command that mutates workspace files: output redirection (not to
# /dev/null or another fd), tee, in-place sed, patch, file copy/move tools.
# Conservative on purpose — a false positive only feeds a nudge counter; a
# false negative is the routing gap this closes.
_SHELL_WRITE_RE = re.compile(
    r"(?<![0-9&])>>?(?![&>])(?!\s*/dev/null)"
    r"|\btee\b"
    r"|\bsed\s+(?:-[a-zA-Z]*i|--in-place)"
    r"|\bpatch\b"
    r"|\b(?:cp|mv|rsync|install|dd|truncate)\s")


def _gate_write_like(name: str, args) -> bool:
    """Does this call do inline implementation work for the delegate/strength
    gates? True for the direct file-writing tools, and for shell exec calls
    whose command writes files (heredocs, redirects, sed -i, cp/mv, ...)."""
    if name in _DELEGATE_GATE_TOOLS:
        return True
    if name not in _EXEC_GATE_TOOLS:
        return False
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return False
    if not isinstance(args, dict):
        return False
    cmd = args.get("command") or args.get("code") or ""
    return bool(_SHELL_WRITE_RE.search(cmd))


def _exec_failure(name: str, result) -> tuple[bool, str | None]:
    """(failed, signature) for execution-style tools. These report command
    failures in their PAYLOAD (ok:false / exit_code != 0), not as tool errors
    — a non-zero exit is normal signal to the model. The signature (tool,
    exit code, normalized last stderr line) is deliberately stable across
    'fix' attempts that hit the same crash: digits and addresses vary between
    builds, the crash doesn't. Exactly the loop the escalation nudge breaks."""
    r = result.result if isinstance(result.result, dict) else {}
    if result.status == "error":
        rc, err = None, str(result.error or "")
    elif r.get("ok") is False or r.get("exit_code") not in (None, 0):
        rc = r.get("exit_code")
        err = str(r.get("stderr") or r.get("error") or "")
    else:
        return False, None
    line = ""
    for ln in reversed(err.strip().splitlines()):
        if ln.strip():
            line = ln.strip()
            break
    norm = re.sub(r"0x[0-9a-fA-F]+", "0x", line.lower())
    norm = re.sub(r"\d+", "#", norm)
    norm = re.sub(r"\s+", " ", norm)[:80]
    return True, f"{name}|{rc}|{norm}"


@dataclass
class TurnGuardContext:
    """Everything a pre-turn/post-tool guard needs beyond RunState: per-run
    plumbing and frozen config, fixed when the loop builds the registries.
    runtime/ctx/stuck_hit/user_message/depth are the run's plumbing; the
    rest are per-run config values parsed once by the loop (see the module
    docstring for which values guards parse themselves)."""
    runtime: Any            # AgentRuntime — Any: loop.py imports this module
    ctx: Any                # the run's ToolContext
    stuck_hit: Any          # the loop's _stuck_hit closure (escalation)
    user_message: str
    depth: int
    warn_fraction: float    # budgets.warn_fraction (pressure nudge trip line)
    ctx_tokens: int         # served context window (0 = context guard off)
    budget_cfg: dict        # the merged budgets: section (b_cfg)
    agent_cfg: dict         # the agent: config section
    lg_cfg: dict            # the loop_guard: config section
    # Parsed by the loop because inline code shares them (see docstring):
    stall_enabled: bool
    stall_after: int
    stall_hard_stop: bool
    fresh_retry_enabled: bool
    fresh_retry_after: int
    delegate_after: int
    delegate_enforce: bool


@dataclass
class PreTurnAction:
    """One pre-turn guard firing: `message` is appended as a system turn
    (None = nothing to append), `events` are (type, data) pairs the loop
    emits in order at the current iteration."""
    message: str | None = None
    events: list = field(default_factory=list)


class PreTurnGuard:
    """Base + contract for one pre-turn guard. `name` is the stable id
    (telemetry). check() may mutate RunState (the one-shot flags live
    there) but never touches rs.messages or emits — the loop applies the
    returned action so ordering stays centralized."""
    name: str = ""

    def __init__(self, tctx: TurnGuardContext) -> None:
        self.tctx = tctx

    async def check(self, rs: RunState) -> PreTurnAction | None:
        raise NotImplementedError


class BudgetPressureGuard(PreTurnGuard):
    """Once the run nears any ceiling, nudge the model to land the plane:
    save progress, leave a resume note, summarize, and stop — instead of
    getting hard-cut mid-edit with nothing usable. One-shot."""
    name = "budget_warning"

    async def check(self, rs: RunState) -> PreTurnAction | None:
        if rs.budget_warned or not self.tctx.warn_fraction:
            return None
        pr, dim = rs.budget.pressure()
        if pr < self.tctx.warn_fraction:
            return None
        rs.budget_warned = True
        return PreTurnAction(
            message=_budget_warning(pr, dim, rs.budget.elapsed_s),
            events=[("budget_warning",
                     {"pressure": round(pr, 2), "dimension": dim,
                      "elapsed_s": round(rs.budget.elapsed_s, 1)})])


class BudgetFinalNoticeGuard(PreTurnGuard):
    """Final notice: a second, blunter one-shot at budget.final_warn_fraction
    of the WALL CLOCK (default 0.95, 0 disables) — the 0.8 checkpoint nudge
    is project-oriented ("save, hand off"), but question-answering runs
    kept researching straight through it and died on the clock with no
    answer at all (live: gaia-dc22a632, 36 web calls, no FINAL ANSWER).
    One blunt "answer NOW" at the wall-clock's last stretch — after this
    there is no next turn to recover in."""
    name = "budget_final_notice"

    async def check(self, rs: RunState) -> PreTurnAction | None:
        if rs.budget_final_warned or not rs.budget.max_wall_clock_s:
            return None
        try:
            final_frac = float(self.tctx.budget_cfg.get("final_warn_fraction",
                                                        0.95) or 0)
        except (TypeError, ValueError):
            final_frac = 0.95
        if not (final_frac and self.tctx.warn_fraction
                and final_frac > self.tctx.warn_fraction):
            return None
        tfr = rs.budget.elapsed_s / rs.budget.max_wall_clock_s
        if tfr < final_frac:
            return None
        rs.budget_final_warned = True
        left = max(0, int(rs.budget.max_wall_clock_s - rs.budget.elapsed_s))
        return PreTurnAction(
            message=(
                "⚠ FINAL NOTICE: about " + str(left) +
                " seconds of run time left — this is the last "
                "chance. Stop ALL tool calls and answer NOW "
                "with the best you have (if the task defines "
                "an answer format, use it exactly). An "
                "imperfect answer beats none."),
            events=[("budget_warning",
                     {"pressure": round(tfr, 2),
                      "dimension": "time-final",
                      "elapsed_s": round(rs.budget.elapsed_s, 1)})])


class ContextPressureGuard(PreTurnGuard):
    """Same one-shot nudge when the PROMPT itself nears the context window —
    distinct from the token BUDGET (cumulative spend); this is about the
    per-turn window filling up. orchestrator.context_tokens 0/unset
    disables (the loop passes the resolved value as tctx.ctx_tokens)."""
    name = "context_warning"

    async def check(self, rs: RunState) -> PreTurnAction | None:
        t = self.tctx
        if (rs.context_warned or not t.warn_fraction or not t.ctx_tokens
                or rs.last_prompt_tokens / t.ctx_tokens < t.warn_fraction):
            return None
        rs.context_warned = True
        cpr = rs.last_prompt_tokens / t.ctx_tokens
        return PreTurnAction(
            message=_context_warning(cpr, t.ctx_tokens),
            events=[("context_warning",
                     {"pressure": round(cpr, 2),
                      "context_tokens": t.ctx_tokens,
                      "prompt_tokens": rs.last_prompt_tokens})])


class StallLadderGuard(PreTurnGuard):
    """Stall ladder: enough consecutive no-progress turns → inject the next
    rung's directive. One-shot per rung; any mutation resets the counter
    (rungs already fired stay fired). agent.stall_check.enabled=false
    disables; 0 `after` disables.

    Stall hard-stop (loop_guard.stall_hard_stop, default on): the rungs only
    NUDGE, and a frozen brain can spin straight through all of them (bakeoff:
    14+ tool calls over 47 minutes past the final warning). Firing the FINAL
    rung therefore also arms rs.stall_hard_stop — the pre-exec dispatch gate
    in loop.py then refuses every tool call but the delegate/ask escape
    hatches until real progress (the ladder's own mutation signal) disarms
    it. The arming event fires here, once, with the rung."""
    name = "stall_check"

    async def check(self, rs: RunState) -> PreTurnAction | None:
        t = self.tctx
        if (not t.stall_enabled or not t.stall_after or rs.wrap_up
                or rs.stall_rung >= len(_STALL_RUNGS)
                or rs.stall_turns < t.stall_after * (rs.stall_rung + 1)):
            return None
        _del = (" Heavy implementation? Call `specialist.delegate` — "
                "the specialist model does the heavy lifting."
                if rs.delegate_ok else "")
        _rung_text = _STALL_RUNGS[rs.stall_rung].format(n=rs.stall_turns,
                                                        delegate=_del)
        # Active procedure? Its checklist is the concrete version of "make
        # progress" — nudge against ITS steps, not just generically
        # (procedure todo step 4).
        if rs.proc_checkpoints:
            _rung_text += (
                f" Active procedure '{rs.proc_name}' — work its "
                "checklist in order, next undone item first: "
                + "; ".join(rs.proc_checkpoints) + ".")
        _rung_text += await t.stuck_hit(
            f"no progress for {rs.stall_turns} turns")
        events = [("stall_check", {"rung": rs.stall_rung + 1,
                                   "turns": rs.stall_turns})]
        rs.stall_rung += 1
        if t.stall_hard_stop and rs.stall_rung >= len(_STALL_RUNGS):
            rs.stall_hard_stop = True
            events.append(("stall_hard_stop",
                           {"armed": True, "turns": rs.stall_turns}))
        return PreTurnAction(message=_rung_text, events=events)


class DeliverableReminderGuard(PreTurnGuard):
    """Deliverable early warning: enough iterations remain to still write
    the files (>= 2), task-named files don't exist yet → remind once. The
    final-answer deliverable check stays the backstop. warn_at: fraction
    of max_iterations (0 disables). Mid-run early warning exists because
    the final-answer check only fires when the model STOPS — a run that
    burns its last iterations still computing never gets to react (live:
    tb-count-dataset-tokens, nudge at the cap, answer.txt never written)."""
    name = "deliverable_warn"

    def __init__(self, tctx: TurnGuardContext) -> None:
        super().__init__(tctx)
        _dcfg = (tctx.agent_cfg.get("deliverable_check") or {})
        self.on = bool(_dcfg.get("enabled", True))
        try:
            self.warn_at = float(_dcfg.get("warn_at", 0.75) or 0)
        except (TypeError, ValueError):
            self.warn_at = 0.75

    async def check(self, rs: RunState) -> PreTurnAction | None:
        if (not self.on or rs.deliverable_warned or not self.warn_at
                or not rs.budget.max_iterations
                or rs.budget.iterations < int(
                    rs.budget.max_iterations * self.warn_at)
                or rs.budget.max_iterations - rs.budget.iterations < 2):
            return None
        _missing = self.tctx.runtime._missing_deliverables(
            self.tctx.ctx, self.tctx.user_message)
        if not _missing:
            return None
        rs.deliverable_warned = True
        remaining = rs.budget.max_iterations - rs.budget.iterations
        return PreTurnAction(
            message=("Deliverable reminder: the task named "
                     + ", ".join(_missing) + " — still not written, "
                     f"{remaining} "
                     "iterations remain. Deliver EARLY: write the file "
                     "with fs.write as soon as it works, then refine; "
                     "a perfect analysis with no file is a failed run."),
            events=[("deliverable_warn", {"missing": _missing,
                                          "remaining": remaining})])


class WrapUpGuard(PreTurnGuard):
    """Loop-guard escalation: after guard_max refusals the model gets ONE
    turn with tools disabled to force the answer it owes. Announced here
    (one-shot) with a findings digest — the run's last tool results, so
    the forced final turn answers FROM the work instead of declaring it
    can't call tools (live: gaia-65afbc8a wasted its only wrap-up turn on
    exactly that)."""
    name = "wrap_up"

    async def check(self, rs: RunState) -> PreTurnAction | None:
        if not rs.wrap_up or rs.wrap_up_noted:
            return None
        rs.wrap_up_noted = True
        _digest = []
        for _m in reversed(rs.messages):
            if _m.get("role") == "tool":
                _digest.append(
                    f"- {_m.get('name') or 'tool'}: "
                    + re.sub(r"\s+", " ",
                             str(_m.get("content") or ""))[:160])
                if len(_digest) >= 3:
                    break
        _digest.reverse()
        _wrap_msg = (
            f"LOOP GUARD: you re-issued blocked duplicate tool calls "
            f"{rs.guard_rejections}×. Tool use is now DISABLED for the "
            "rest of this run. Give your final answer immediately "
            "from the results already gathered — say plainly what "
            "you found and what you could not verify.")
        if _digest:
            _wrap_msg += ("\n\nYour most recent findings:\n"
                          + "\n".join(_digest)
                          + "\nDo not reply that you cannot call "
                            "tools — the findings above are your "
                            "evidence; answer best-effort from them.")
        return PreTurnAction(
            message=_wrap_msg,
            events=[("progress",
                     {"label": f"loop guard: {rs.guard_rejections} blocked duplicates "
                               "— tools off, forcing the final answer",
                      "type": "guard"})])


#: The pre-turn guard chain, in firing order (the historical order of the
#: inline blocks at the top of the loop). ORDER IS LOAD-BEARING; append new
#: guards, never reorder silently.
PRE_TURN_GUARDS: list[type[PreTurnGuard]] = [
    BudgetPressureGuard,
    BudgetFinalNoticeGuard,
    ContextPressureGuard,
    StallLadderGuard,
    DeliverableReminderGuard,
    WrapUpGuard,
]


@dataclass
class ToolCallView:
    """One executed (or pre-rejected) tool call as the post-tool guards see
    it: parsed args (None for calls rejected before parsing), the result,
    and whether the pre-exec fresh-retry gate rewrote this call."""
    name: str
    args: Any
    result: Any
    fresh_retry: bool = False


class PostToolGuard:
    """Base + contract for one post-tool guard. check() returns None when
    the guard did not fire on this call, else its hint text (possibly ""
    — see VerifyArmGuard). The loop appends the hint to the tool-result
    content at the guard's `slot` (legacy concatenation order, see
    _POST_TOOL_HINT_SLOTS) and counts the fire for telemetry."""
    name: str = ""
    slot: str = ""

    def __init__(self, tctx: TurnGuardContext) -> None:
        self.tctx = tctx

    async def check(self, rs: RunState, call: ToolCallView) -> str | None:
        raise NotImplementedError


class FailureStreakGuard(PostToolGuard):
    """Crash/failure-loop escalation: execution tools (code.run/code.execute)
    report command failures in their PAYLOAD (ok:false / exit_code!=0), not
    as tool errors — so a crash-retry loop (a segfaulting solver rebuilt
    70× in a live bench run) trips no duplicate guard. Track consecutive
    same-signature failures; at the threshold the tool result gets a
    strategy-change hint appended. 0 disables.
    Payload failures (ok:false / exit_code!=0) are only meaningful signal
    for execution tools; a HARD tool error (status=error) is never
    productive to retry unchanged, so those are tracked for EVERY tool."""
    name = "failure_streak"
    slot = "fail"

    def __init__(self, tctx: TurnGuardContext) -> None:
        super().__init__(tctx)
        try:
            self.after = int(tctx.lg_cfg.get("failure_nudge_after", 3) or 0)
        except (TypeError, ValueError):
            self.after = 3
        self.tools = set(tctx.lg_cfg.get("failure_nudge_tools")
                         or ["code.run", "code.execute", "code.check"])

    async def check(self, rs: RunState, call: ToolCallView) -> str | None:
        if not self.after:
            return None
        name, result = call.name, call.result
        if name in self.tools or result.status == "error":
            failed, sig = _exec_failure(name, result)
            if failed:
                rs.fail_count = rs.fail_count + 1 if sig == rs.fail_sig else 1
                rs.fail_sig = sig
            else:
                rs.fail_sig, rs.fail_count = None, 0
        elif result.status == "ok":
            # Any healthy result breaks the streak — the model did
            # something else that worked, the loop is over.
            rs.fail_sig, rs.fail_count = None, 0
        if rs.fail_count < self.after:
            return None
        if name in self.tools:
            _del = (" Heavy implementation? `specialist.delegate` "
                    "hands it to the specialist model — "
                    "that is what it is for."
                    if rs.allowed is None
                    or not _DELEGATE_TOOLS.isdisjoint(rs.allowed)
                    else "")
            fail_hint = (
                f"\n\n[system note] {rs.fail_count} "
                "consecutive executions failed with the "
                "same error signature. Do NOT retry the "
                "same approach again — change strategy: "
                "simplify, switch algorithm or language, "
                f"verify on a tiny input first.{_del}")
        else:
            fail_hint = (
                f"\n\n[system note] {rs.fail_count} "
                f"consecutive `{name}` calls failed with "
                "the same error — re-issuing the same "
                "call will keep failing. Check the "
                "arguments against reality (does the "
                "job/server/file exist?), switch tools, "
                "or ask the user.")
        if fail_hint:
            fail_hint += await self.tctx.stuck_hit(
                f"{rs.fail_count}× same-signature `{name}` failure")
        return fail_hint


class HostGiveUpGuard(PostToolGuard):
    """Diminishing returns per HOST: the same-signature streak misses the
    loop where every call has DIFFERENT args but the same target — live:
    gaia-4b6bb5f7 burned 44 calls on Scribd timeouts/login walls. Track
    consecutive hard errors or thin render walls per URL host; at the
    threshold every further result from that host carries a give-up hint.
    A healthy result from the host resets its counter. 0 disables."""
    name = "host_give_up"
    slot = "host"

    def __init__(self, tctx: TurnGuardContext) -> None:
        super().__init__(tctx)
        try:
            self.after = int(tctx.lg_cfg.get("host_give_up_after", 4) or 0)
        except (TypeError, ValueError):
            self.after = 4

    async def check(self, rs: RunState, call: ToolCallView) -> str | None:
        if not self.after:
            return None
        _url = (call.args or {}).get("url")
        _host = (urlparse(_url).hostname or "").lower() \
            if isinstance(_url, str) and _url else ""
        if not _host:
            return None
        result = call.result
        _bad = result.status == "error" or (
            isinstance(result.result, dict)
            and bool(result.result.get("thin")))
        if not _bad:
            if result.status == "ok":
                rs.host_fails.pop(_host, None)
            return None
        _n = rs.host_fails.get(_host, 0) + 1
        rs.host_fails[_host] = _n
        if len(rs.host_fails) > 20:      # bound the map
            rs.host_fails.pop(next(iter(rs.host_fails)))
        if _n < self.after:
            return None
        host_hint = (
            f"\n\n[system note] {_host} has now "
            f"failed {_n} times in a row (blocked, "
            "timing out, or returning thin shells) "
            "— this source is unreachable from here "
            "right now. STOP retrying it: get the "
            "data from a different source, or "
            "report the gap to the user and finish "
            "with what you have.")
        host_hint += await self.tctx.stuck_hit(
            f"`{_host}` unreachable ×{_n}")
        return host_hint


class VerifyArmGuard(PostToolGuard):
    """Arming bookkeeping for the final-answer bounces (no hint text — its
    fire IS the state change, reported as an empty hint so telemetry sees
    it): the first tool turn of the run (just-reply input), web/arxiv/
    browser call counting (stuck-escalation input), the delegate/check
    markers the verify-the-delegate bounce reads, and the fresh-retry
    outcome bookkeeping (every delegation's outcome recorded against its
    task-signature cluster, so the pre-exec gate can de-anchor a
    repeatedly failing task; failure = tool error, a non-ok child status,
    or a verify check that came back False)."""
    name = "verify_arm"
    slot = "arm"

    async def check(self, rs: RunState, call: ToolCallView) -> str | None:
        name, args, result = call.name, call.args, call.result
        fired = False
        if isinstance(name, str) and name.startswith(
                ("web.", "arxiv.", "browser.")):
            rs.web_calls += 1
        if rs.any_tool_turn < 0:
            rs.any_tool_turn = rs.budget.iterations
            fired = True
        if name in _DELEGATE_TOOLS:
            rs.delegated = True
            fired = True
            # Arm the verify bounce for implementation-shaped delegations
            # (coding default, multi-step) — research hand-offs verify
            # differently than code.check.
            _st = str((args or {}).get("strength") or "coding")
            if _st in ("coding", "multi-step"):
                rs.delegate_turn = rs.budget.iterations
        elif name in _CHECK_TOOLS:
            rs.check_turn = rs.budget.iterations
            fired = True
        t = self.tctx
        if (t.fresh_retry_enabled
                and (name in _DELEGATE_TOOLS or name == "agent.spawn")
                and isinstance(args, dict)
                and isinstance(args.get("task"), str)):
            _tok = t.runtime._arg_tokens({"task": args["task"]})
            _tr = next(
                (tr for tr in rs.delegate_trials
                 if t.runtime._jaccard(tr["tokens"], _tok) >= 0.5),
                None)
            if _tr is None:
                _tr = {"tokens": _tok, "failures": 0,
                       "fresh": call.fresh_retry}
                rs.delegate_trials.append(_tr)
            _res = result.result if isinstance(result.result, dict) else {}
            if (result.status != "ok"
                    or _res.get("status") not in (None, "ok")
                    or _res.get("verified") is False):
                _tr["failures"] += 1
            if call.fresh_retry and isinstance(result.result, dict):
                result.result["fresh_retry"] = (
                    "de-anchored retry: after repeated failures the "
                    "child received the ORIGINAL request, not your "
                    "task framing, and no orientation pack")
        return "" if fired else None


class DelegateNudgeGuard(PostToolGuard):
    """Delegate gate (soft mode): count successful inline write/edit calls
    while specialist.delegate is available but unused. At the threshold,
    direct the brain to hand the implementation over; in enforce mode the
    2x mark is the final warning (further inline edits are rejected
    pre-exec by the inline gate). The directive rides the tool result;
    in enforce mode the threshold write is already rejected pre-exec —
    the rejection IS the message."""
    name = "delegate_nudge"
    slot = "delegate"

    async def check(self, rs: RunState, call: ToolCallView) -> str | None:
        t = self.tctx
        if (not t.delegate_after or t.depth != 0 or rs.delegated
                or not _gate_write_like(call.name, call.args)
                or call.result.status != "ok"
                or not rs.delegate_ok):
            return None
        rs.inline_writes += 1
        if t.delegate_enforce or rs.inline_writes < t.delegate_after:
            return None
        return (
            "\n\n[system note] You have made several "
            "inline file edits — this is non-trivial "
            "coding, which belongs with the "
            "specialist. Call `specialist.delegate` with a "
            "complete, standalone task (the heavy "
            "transcript stays in the child's context, "
            "not yours), then verify its report.")


class BadgeWatchGuard(PostToolGuard):
    """Badge watch: a skill with `requires_badge: true` in frontmatter asks
    the model to badge the run (run.badge) after loading — j-space's eval
    history shows the badge step is chronically skipped (12+ of 19 runs)
    even when everything else goes right. After such a skill loads, the
    first file-edit tool gets a one-shot reminder until a run.badge call
    lands. The frontmatter flag is the switch. Prompt placement alone
    doesn't get small brains to badge (j-space evals)."""
    name = "badge_watch"
    slot = "badge"

    async def check(self, rs: RunState, call: ToolCallView) -> str | None:
        name, args, result = call.name, call.args, call.result
        if name == "run.badge" and result.status == "ok":
            rs.badged = True
        if (name == "skill.load" and result.status == "ok"
                and isinstance(args, dict)):
            try:
                from runtime import paths as _paths
                from runtime.skills import discover_skills_layered_cached
                _skdir = (self.tctx.runtime.config.get("skills") or {}).get(
                    "dir", str(_paths.SKILLS_DIR))
                _sk = discover_skills_layered_cached(
                    _skdir, _paths.CUSTOM_SKILLS_DIR
                ).get(str(args.get("name") or ""))
                if _sk and _sk.get("requires_badge"):
                    rs.badge_watch = _sk["name"]
            except Exception:
                pass
        if (not rs.badge_watch or rs.badged or rs.badge_nudged
                or not _gate_write_like(name, args)
                or result.status != "ok"):
            return None
        rs.badge_nudged = True
        return (
            f"\n\n[system note] You loaded `{rs.badge_watch}`, "
            "which asks you to badge the run before file "
            "work — call `run.badge` with the pass label "
            "now, then continue.")


#: The post-tool guard chain, in SIDE-EFFECT order (the historical order of
#: the inline blocks). The hint TEXT reassembles in the legacy fail →
#: delegate → badge → host order via each guard's `slot`:
#: ORDER IS LOAD-BEARING; append new guards, never reorder silently.
POST_TOOL_GUARDS: list[type[PostToolGuard]] = [
    FailureStreakGuard,
    HostGiveUpGuard,
    VerifyArmGuard,
    DelegateNudgeGuard,
    BadgeWatchGuard,
]

#: Legacy tool-result content order: the inline era appended
#: fail_hint + delegate_hint + badge_hint + host_hint. The loop
#: reassembles fired hints in exactly this order (byte-identical content).
_POST_TOOL_HINT_SLOTS = ("fail", "delegate", "badge", "host")
