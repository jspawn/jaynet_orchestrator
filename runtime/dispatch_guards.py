"""Dispatch-phase gates and their shared per-run context.

Extracted from AgentRuntime.run() (audit #1, phase 1): the pre-execution
tool-call gates that decide whether a model-emitted tool call may run, plus
the helper closures those gates call. The gate CLASSES themselves are the
classes at the bottom, registered in DISPATCH_GATES in exact historical
inline order — order is load-bearing (see the registry comment).

Contract (mirrors turn_guards.py, adapted for per-call dispatch):

- Each gate's `check(plan, rs)` returns True when it SETTLED the call
  (plan["result"] is set — the pipeline stops for this call), False to fall
  through. Gates may mutate RunState and plan, and emit via self.dctx.emit —
  this preserves the historical inline event ordering byte-for-byte.
- `plan` is the accumulator the post-execution block consumes; keys: tc, fn,
  raw_args, name, args, result, guard_refused, fresh_retry, privacy_ok.
- Only the escalation gates (stall hard-stop, repeat-error block, j-space
  badge, duplicate, near-duplicate) feed rs.guard_rejections/wrap_up; the
  delegate-pointing gates (strength, dispatch-mode, delegate-hard) feed only
  rs.delegate_refusals/auto-delegate. Keep that asymmetry.
- guard_fired telemetry: only stall_hard_stop and jspace_badge_gate emit
  guard_fired (phase "dispatch") — historically the others emit legacy
  events (repeat_blocked, fresh_retry) or nothing. Do not add more.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .tool_base import ToolResult
from .turn_guards import _DELEGATE_TOOLS, _gate_write_like


def _strength_kw_hit(kw: str, msg: str) -> bool:
    """Strength-keyword match: word-boundary for short acronyms (<=4 chars,
    no space — "rce", "cve", "xss"), substring otherwise so stems like "vuln"
    still catch "vulnerable"/"vulnerability"."""
    if len(kw) <= 4 and " " not in kw:
        return bool(re.search(r"\b" + re.escape(kw) + r"\b", msg))
    return kw in msg


# Default strength-domain keywords (routing nudge + strength gate). Config
# tool_selection.routing_nudge.strength_keywords overrides; short acronyms
# match on word boundaries via _strength_kw_hit ("rce" must not fire inside
# "source"), longer keywords stay substring so stems work.
_DEFAULT_STRENGTH_KEYWORDS = {
    "security": ["vulnerability", "vuln", "exploit", "pentest", "pen test",
                 "cve", "sql injection", "xss", "privilege escalation",
                 "malware", "forensic", "security audit", "rce",
                 "reverse shell", "intrusion", "incident response",
                 "security threat", "threat detection", "capture the flag"],
}


def _traj_arg_hint(args: dict | None) -> str:
    """A short, non-sensitive hint of what a tool call was aimed at — taken from
    the call's *arguments* (the model's own inputs: a URL, query, path, model,
    collection), never from the result, so trajectory notes can't leak private
    tool output back into replayed history. `args` is None for calls rejected
    before parsing (allowlist / invalid-JSON gates) — hintless, not a crash.
    A parsed-but-non-dict payload (the model emitted a JSON list/string) is
    hintless too."""
    if not isinstance(args, dict):
        return ""
    for k in ("url", "query", "path", "task", "model", "collection", "name"):
        v = args.get(k)
        if v:
            s = str(v).replace("\n", " ").strip()
            return s[:70] + ("…" if len(s) > 70 else "")
    return ""


def _traj_entry(name: str, args: dict, result) -> str:
    """One compact trajectory line: tool(hint)->status[: error]."""
    hint = _traj_arg_hint(args)
    head = f"{name}({hint})" if hint else name
    if result.status == "ok":
        return f"{head}→ok"
    return f"{head}→{result.status}: {(result.error or '')[:80]}"


Emit = Callable[[str, int, dict], Awaitable[None]]


@dataclass
class DispatchGateContext:
    """Per-run context for the dispatch gates: the run's plumbing plus the
    config snapshots the gates share (parsed once by the loop because inline
    code uses them too). Also owns the escalation helpers the gates call —
    the former run() closures, as methods (TurnGuardContext carries the
    stuck_hit closure the same way)."""

    runtime: Any                  # AgentRuntime
    ctx: Any                      # ToolContext
    rs: Any                       # RunState
    emit: Emit
    user_message: Any
    depth: int
    delegate_available: bool
    auto_delegate_after: int
    stuck_after: int
    jspace_badge_gate_on: bool
    # Gate config snapshots + run plumbing (parsed once by the loop because
    # inline code shares them).
    is_admin: bool = False
    admin_only_names: set = field(default_factory=set)
    guard_max: int = 0
    hard_block_after: int = 0
    stall_hard_stop_on: bool = False
    dispatch_gate: bool = False
    delegate_after: int = 0
    delegate_enforce: bool = False
    delegate_escalate: bool = False
    brain_gate: bool = False
    fresh_retry_enabled: bool = False
    fresh_retry_after: int = 0
    near_dup_tools: set = field(default_factory=set)
    near_dup_threshold: float = 0.0
    share_private: bool = False
    auto_confirm: bool = False
    run_id: Any = None
    confirm_provider: Any = None

    # ---- delegate routing (was: _delegate_candidates/_pick_delegate_route) --

    def _delegate_candidates(self) -> list[str]:
        """Strength tags to try for a hand-over, in priority order:
        request keywords, then dominant tool activity, then the generic
        fallbacks. Shared by the stuck-delegate directive and the
        auto-delegate hand-over."""
        rs = self.rs
        msg = self.user_message if isinstance(self.user_message, str) else ""
        _skw = (((self.runtime.config.get("tool_selection") or {})
                 .get("routing_nudge") or {}).get("strength_keywords")
                or _DEFAULT_STRENGTH_KEYWORDS)
        cands = [tag for tag, kws in _skw.items()
                 if any(_strength_kw_hit(k, msg) for k in kws)]
        if rs.inline_writes > rs.web_calls:
            cands.append("coding")
        if rs.web_calls:
            cands.append("research")
        cands += ["multi-step", "coding", "research", "allround"]
        return cands

    async def pick_delegate_route(self) -> tuple[str, dict] | None:
        """First candidate tag with a live/swappable route, else None."""
        from tools.model.catalog import strength_route
        seen_c: set[str] = set()
        for cand in self._delegate_candidates():
            if cand in seen_c:
                continue
            seen_c.add(cand)
            try:
                plan = await strength_route(self.runtime.config, cand)
            except Exception:
                plan = {}
            if plan:
                return (cand, plan)
        return None

    # ---- escalation helpers (was: _auto_delegate & friends) ----------------

    async def auto_delegate(self, reason: str) -> bool:
        """Harness-side specialist.delegate after a refusal streak. True
        when the delegation ran OK — a successful hand-over is real
        progress: it disarms the delegate/strength/stall gates and cancels
        a pending wrap-up."""
        rs = self.rs
        if (not self.auto_delegate_after or rs.auto_delegated or rs.delegated
                or self.depth != 0 or not self.delegate_available):
            return False
        # Wall clock spent? A child's budget is clamped to the parent's
        # REMAINING allowance, so the spawn inside specialist.delegate would
        # be refused — but only AFTER the delegate tool burned a model swap
        # (up to swap_wait_s) getting there (eval gaia-cca530fc 2026-10-08:
        # 125s swap, then the refusal). Skip straight to wrap-up instead.
        if (rs.budget.max_wall_clock_s
                and rs.budget.elapsed_s >= rs.budget.max_wall_clock_s):
            await self.emit("progress", rs.budget.iterations, {
                "label": "loop guard: skipping auto-delegate — the run's "
                         "wall-clock budget is spent; wrapping up",
                "type": "guard"})
            return False
        route = await self.pick_delegate_route()
        if not route:
            return False
        tag, route_plan = route
        tool_name = next(
            (t for t in ("specialist.delegate", "code.delegate")
             if self.runtime.registry.get(t) is not None
             and (rs.allowed is None or t in rs.allowed)), None)
        if tool_name is None:
            return False
        # j-space ceremony salvage (j-space-loop live validation
        # 2026-10-05 rep 1): this harness-side delegation bypasses the
        # model-call dispatch where the j-space badge gate lives, so a
        # stalled badged-but-planless j-space run got an UNPLANNED
        # specialist implementation — exactly what the gate exists to
        # prevent. When the gate is armed and unlatched, close BOTH
        # openers harness-side before delegating and latch the gate —
        # with honest attribution: the badge event and the salvage plan
        # are the loop guard's, never the brain's (the judge and the
        # user must be able to tell the brain never planned). The latch
        # also keeps the brain's post-delegation verify step from being
        # rejected (code.check can trip _gate_write_like via test-side
        # pyc writes).
        _jg_ceremony = False
        if (self.jspace_badge_gate_on and rs.badge_watch == "j-space"
                and not rs.jspace_gate_open):
            if not rs.badged:
                rs.badged = True
                await self.emit("badge", rs.budget.iterations,
                                {"label": "j-space: full"})
                _jg_ceremony = True
            if not (rs.todo_list.items or rs.todo_list.requirements):
                await self.todos_update({"action": "set", "items": [
                    {"title": f"Delegate the stalled task to the {tag} "
                              "specialist",
                     "desc": "loop guard salvage — the brain stalled "
                             "before planning"},
                    {"title": "Verify the specialist's result"},
                    {"title": "Answer the original request"}]})
                _jg_ceremony = True
            rs.jspace_gate_open = True
            if _jg_ceremony:
                await self.emit("progress", rs.budget.iterations, {
                    "label": "loop guard: closed the j-space ceremony "
                             "harness-side (badge + salvage plan) before "
                             "auto-delegating",
                    "type": "guard"})
        rs.auto_delegated = True   # latch even on failure — no retry loop
        brief = ("AUTO-DELEGATED BY THE LOOP GUARD — the orchestrator "
                 f"stalled ({reason}) and ignored repeated delegate "
                 "directives. Solve the ORIGINAL request below from "
                 "scratch; do not assume any of its intermediate files "
                 "or attempts are correct.\n\nORIGINAL REQUEST:\n"
                 + ((self.user_message or "")[:6000]
                    if isinstance(self.user_message, str) else reason))
        args = {"task": brief, "strength": tag}
        result = await self.runtime._execute_tool(tool_name, args, self.ctx)
        ok = result.status == "ok"
        _pcap = int((self.runtime.config.get("web", {}) or {})
                    .get("tool_preview_chars", 8000))
        await self.emit("tool_result", rs.budget.iterations, {
            "tool": tool_name,
            "args": {"task": brief[:200] + "…", "strength": tag},
            "status": result.status, "error": result.error,
            "result_preview": (result.to_model_message()[:_pcap]
                               if ok else None),
            "latency_ms": result.latency_ms,
            "tokens": result.tokens_used, "private": result.private})
        rs.trajectory.append(_traj_entry(tool_name, args, result))
        rs.tools_used.append(tool_name)
        if ok:
            rs.mutation_gen += 1
            rs.delegated = True         # disarms delegate/strength gates
            rs.stall_hard_stop = False  # the report IS the progress
            # …and the ladder must see it too (skill-load delta-fail
            # 2026-10-07, trace e27ec9e4): this harness-side call runs in
            # the dispatch phase, BEFORE _mg_before is snapshotted for
            # the end-of-turn accounting, so the mutation_gen bump above
            # is invisible there and the turn that produced the complete
            # deliverable kept counting as no-progress — the ladder
            # killed a run the hand-over had finished.
            rs.stall_turns = 0
            # Mirror VerifyArmGuard's verified-completion marker (the
            # harness-side call bypasses the post-tool guards — the
            # comment below admits as much for delegate_turn): verified
            #:true → later rungs say "answer now" (_STALL_WRAPUP)
            # instead of "produce something" to a run that already did.
            # Set-only (once verified, stays verified), branch-free to
            # keep run() under the complexity ceiling.
            rs.delegate_verified |= (isinstance(result.result, dict)
                                     and result.result.get("verified") is True)
            # Mirror the verify-arm post-tool guard (the harness-side call
            # bypasses it): implementation-shaped hand-overs still owe a
            # verification before the final answer.
            if tag in ("coding", "multi-step"):
                rs.delegate_turn = rs.budget.iterations
        report = (result.to_model_message()[:4000] if ok
                  else f"delegation failed: {result.error}")
        rs.messages.append({"role": "system", "content": (
            "[loop guard] Blocked-call streak — the harness delegated the "
            f"remaining work to the {tag} specialist itself.\n"
            f"Specialist report:\n{report}\n"
            "Continue from this result; do NOT retry the blocked "
            "approach."
            + (" The j-space badge/plan ceremony on this run was "
               "performed by the loop guard, not the brain — the brain "
               "stalled before planning."
               if _jg_ceremony else ""))})
        await self.emit("progress", rs.budget.iterations, {
            "label": f"loop guard: auto-delegated to the {tag} "
                     f"specialist ({reason})",
            "type": "guard"})
        await self.emit("auto_delegate", rs.budget.iterations,
                        {"reason": reason, "strength": tag, "tool": tool_name,
                         "status": result.status,
                         "mode": route_plan.get("mode")})
        return ok

    async def wrap_up_or_salvage(self, reason: str) -> None:
        """guard_max reached: before tools go off for the wrap-up turn,
        one harness-side delegation attempt. Success = progress, the run
        continues with the specialist report in context."""
        if not await self.auto_delegate(reason):
            self.rs.wrap_up = True

    async def stuck_hit(self, source: str) -> str:
        """Record a distress signal; once the run crosses the stuck
        threshold, return the concrete hand-over directive ('' before
        that, when disabled, or when nothing routes)."""
        rs = self.rs
        if (not self.stuck_after or rs.stuck_fired or rs.delegated
                or self.depth != 0 or not self.delegate_available):
            return ""
        rs.stuck_signals.append(source)
        if len(rs.stuck_signals) < self.stuck_after:
            return ""
        route = await self.pick_delegate_route()
        if not route:
            return ""
        rs.stuck_fired = True
        tag, plan = route
        mode = ("live right now" if plan.get("mode") == "live"
                else "loadable on demand")
        return ("\n\n[system note] You are stuck ("
                + "; ".join(rs.stuck_signals[-3:]) + "). Stop retrying "
                "solo — hand this over NOW:\n"
                f"specialist.delegate(task=\"<your current goal in one "
                "or two sentences, including file paths/URLs you already "
                f"found>\", strength=\"{tag}\")\n"
                f"The {tag} specialist is {mode}. When it returns, "
                "continue with its result instead of retrying the "
                "approach that just failed.")

    async def todos_update(self, payload: dict) -> dict:
        rs = self.rs
        res = rs.todo_list.apply(payload)
        if res.get("status") == "ok":
            snap = rs.todo_list.snapshot()
            reqs = list(rs.todo_list.requirements)
            if snap != rs._last_todos_emit[0] or reqs != rs._last_reqs_emit[0]:
                rs._last_todos_emit[0] = snap
                rs._last_reqs_emit[0] = reqs
                await self.emit("todos", rs.budget.iterations,
                                {"items": snap, "requirements": reqs})
        return res


# ---- the gates ---------------------------------------------------------------
# (Commit B lands the 16 gate classes here, in exact historical inline order.)

# ---- moved classification helpers (shared with loop.py via re-export) -------

# Source-file targets the dispatch gate rejects (fs.write/fs.edit). Prose,
# config and data files stay writable — the brain still takes notes, writes
# reports and edits its own configs. Extension match plus the well-known
# extension-less build files.
_CODE_FILE_EXTS = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue",
    ".svelte", ".rs", ".go", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
    ".java", ".kt", ".kts", ".scala", ".rb", ".php", ".cs", ".fs", ".fsx",
    ".vb", ".swift", ".m", ".mm", ".lua", ".pl", ".pm", ".r", ".jl", ".ex",
    ".exs", ".erl", ".hrl", ".hs", ".ml", ".mli", ".sh", ".bash", ".zsh",
    ".ps1", ".bat", ".cmd", ".sql", ".html", ".htm", ".css", ".scss",
    ".less",
})
_CODE_FILE_NAMES = frozenset({
    "dockerfile", "makefile", "cmakelists.txt", "jenkinsfile", "rakefile",
    "gemfile", "vagrantfile", "brewfile",
})


def _code_file_target(args) -> bool:
    """True when fs.write/fs.edit args target a source-code path."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return False
    if not isinstance(args, dict):
        return False
    path = str(args.get("path") or "").strip().lower()
    if not path:
        return False
    base = path.rsplit("/", 1)[-1]
    if base in _CODE_FILE_NAMES:
        return True
    dot = base.rfind(".")
    return dot > 0 and base[dot:] in _CODE_FILE_EXTS


def _jspace_ledger_target(args) -> bool:
    """True when fs.write/fs.edit args target the j-space ledger itself
    (<workspace>/.jspace/...) — the skill maintains that file with the fs.*
    tools as part of its protocol, so the badge gate (loop_guard.
    jspace_badge_gate) exempts it: blocking the ledger would break the
    legitimate flow the gate exists to protect."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return False
    if not isinstance(args, dict):
        return False
    path = str(args.get("path") or "").replace("\\", "/")
    return path.startswith(".jspace/") or "/.jspace/" in path


# Stall hard-stop escape hatches (loop_guard.stall_hard_stop): once the
# final stall-ladder rung has armed rs.stall_hard_stop, the pre-exec
# dispatch gate refuses every tool call EXCEPT these — delegate the
# remaining work (both delegate verbs, like the other delegate gates) or
# ask the user. The pure-answer path needs no entry: giving the final
# answer is stopping tool calls, not making one.
# Bookkeeping (todos/pin/badge) also passes: refusing it doesn't stop a
# spin — the model just retries and burns iterations (live: bonsai
# code-refactor, 8 refused todos retries after the work was done blew the
# eval iteration cap) — and it can never disarm the stop or mask a stall,
# because it is in _NO_PRODUCT_TOOLS (the ladder's counter keeps climbing
# toward wrap-up either way).
_BOOKKEEPING_TOOLS = frozenset({"todos", "context.pin", "run.badge"})
# fs.write/fs.edit also pass (delta-fail investigation 2026-10-06,
# tb-regex-log): six fresh diagnostic code.check turns armed the stop, then
# the write carrying the DIAGNOSED fix was refused and wrap_up forced an
# answer stating a fix it couldn't apply. A real write disarms via the
# ladder's own mutation reset; byte-identical rewrites still don't (the
# _no_change/repeat bookkeeping never resets on them, and the duplicate
# guard refuses exact repeats). code.check stays blocked on purpose: an
# armed stop exists to close verify-spin loops, not to feed them.
_STALL_HARD_STOP_OK = (_DELEGATE_TOOLS | {"ask.user"} | _BOOKKEEPING_TOOLS
                       | {"fs.write", "fs.edit"})


class DispatchGate:
    """One pre-execution tool-call gate. check() returns True when the gate
    SETTLED the call (plan["result"] set — stop the pipeline), False to fall
    through to the next gate. Gates mutate RunState and plan directly and
    emit via self.dctx.emit — preserving the historical inline ordering.

    `ablatable` marks gates an eval benchmark variant may switch off via
    guards_off. The structural gates (malformed/allowlist/parse) and the
    human-approval gates (privacy/confirmation) are NEVER ablatable."""

    name: str = ""
    ablatable: bool = False

    def __init__(self, dctx: DispatchGateContext):
        self.dctx = dctx

    async def check(self, plan: dict, rs) -> bool:  # noqa: ARG002
        return False


class MalformedCallGate(DispatchGate):
    """Missing/invalid tool name — hand the model an error result it can
    recover from, never a run-ending crash. Must run first: nothing else is
    computable without a name."""

    name = "malformed_call"

    async def check(self, plan: dict, rs) -> bool:
        name = plan["name"]
        if isinstance(name, str) and name:
            return False
        plan["name"] = "<malformed>"
        plan["result"] = ToolResult(
            status="error", result=None,
            error=f"malformed tool call from model: "
                  f"{repr(plan['fn'] or plan['tc'])[:200]}")
        return True


class AdminOnlyGate(DispatchGate):
    """Role policy at execution time, not just selection:
    security.admin_only_tools (host shell, job/serve lifecycle, model
    swaps, git push, MCP, scheduling) is refused for non-admin runs even if
    a selection path let the name through. Checked BEFORE the allowlist so
    the model sees WHY the tool is unavailable."""

    name = "admin_only"

    async def check(self, plan: dict, rs) -> bool:
        name = plan["name"]
        if self.dctx.is_admin or name not in self.dctx.admin_only_names:
            return False
        plan["result"] = ToolResult(
            status="error", result=None, tool_name=name,
            error=f"tool '{name}' is admin-only — this account "
                  "is not an administrator")
        return True


class AllowlistGate(DispatchGate):
    """The selected allowlist is a hard boundary, not just an exposure hint.
    Matters most for sub-agents — a research child literally cannot execute
    fs.write even if it tries."""

    name = "allowlist"

    async def check(self, plan: dict, rs) -> bool:
        name = plan["name"]
        if rs.allowed is None or name in rs.allowed:
            return False
        plan["result"] = ToolResult(
            status="error", result=None, tool_name=name,
            error=f"tool '{name}' is not permitted in this run")
        return True


class StallHardStopGate(DispatchGate):
    """Stall hard-stop (loop guard): the FINAL stall-ladder rung fired and
    nothing has mutated since — the brain is spinning, so tool calls are
    closed except the escape hatches (delegate the work, ask the user) and
    the pure-answer path. Refused AT CALL TIME: the exposed tool list is
    never shrunk mid-run — the chat template renders tools before history,
    so changing them would invalidate the whole prompt-cache prefix."""

    name = "stall_hard_stop"
    ablatable = True

    async def check(self, plan: dict, rs) -> bool:
        d = self.dctx
        name = plan["name"]
        if not (d.stall_hard_stop_on and rs.stall_hard_stop
                and name not in _STALL_HARD_STOP_OK):
            return False
        rs.guard_rejections += 1
        rs.delegate_refusals += 1
        # Auto-delegate (loop_guard.auto_delegate_after): the refusal NAMES
        # the escape hatch but a frozen brain retries the blocked call
        # instead (bakeoff blind-spot autopsy) — at the threshold the
        # harness delegates itself. A success is real progress: it disarms
        # the stop and cancels the wrap-up.
        _salvaged = False
        if (d.auto_delegate_after
                and rs.delegate_refusals >= d.auto_delegate_after):
            _salvaged = await d.auto_delegate(
                "stall hard-stop refusal streak")
        if not _salvaged and (
                d.guard_max and rs.guard_rejections >= d.guard_max
                or (d.auto_delegate_after
                    and rs.delegate_refusals >= d.auto_delegate_after)):
            # Endgame: the refusal streak hit the auto-delegate threshold
            # and nothing salvaged the run (no delegate route, or it
            # failed) — OR the general rejection cap blew. Don't keep
            # refusing tools until the iteration cap kills the run with no
            # answer (live: house-search child, 2 blocked fetches after the
            # hard stop, then "[Run terminated] (no answer produced yet)").
            # Tools go OFF next turn: forced synthesis from what the run
            # already gathered.
            rs.wrap_up = True
        plan["guard_refused"] = True
        # The FIRST refusal explains; repeats get the minimal string — at
        # refusal-streak depth the long text is context poison, not
        # information.
        _err = (
            f"BLOCKED (stall hard-stop: no progress in "
            f"{rs.stall_turns} turns). Next: "
            "specialist.delegate(task=…), ask.user, or your "
            "final answer."
            if rs.delegate_refusals > 1 else
            f"stalled (stall_hard_stop guard): no "
            f"progress in {rs.stall_turns} turns. Tool "
            "calls are closed now except writes: if you "
            "have a DIAGNOSED fix, apply it with "
            "fs.write/fs.edit (a real change re-opens "
            "tools). Otherwise: delegate the "
            "remaining work (specialist.delegate), ask "
            "the user (ask.user), or give your final "
            "answer.")
        plan["result"] = ToolResult(
            status="error", result=None, tool_name=name,
            error=_err)
        await d.emit("guard_fired", rs.budget.iterations,
                     {"name": "stall_hard_stop",
                      "phase": "dispatch",
                      "turn": rs.budget.iterations})
        return True


class RepeatErrorBlockGate(DispatchGate):
    """Repeat-error hard block (loop guard): this EXACT call already failed
    hard_block_repeat_errors times with the same error — refusing beats
    re-running a deterministic failure. Checked BEFORE the strength/
    dispatch/delegate gates so repeats of THEIR rejections (the
    gaia-e142056d loop) are caught too: those calls never reach the
    duplicate guard, so without this block they can be re-issued forever."""

    name = "repeat_error_block"
    ablatable = True

    async def check(self, plan: dict, rs) -> bool:
        d = self.dctx
        name = plan["name"]
        if not d.hard_block_after:
            return False
        _errs = rs.repeat_fails.get(
            (name, d.runtime._repeat_args_sig(name, plan["raw_args"])))
        if not _errs:
            return False
        _eid, _cnt = max(_errs.items(), key=lambda kv: kv[1])
        if _cnt < d.hard_block_after:
            return False
        rs.guard_rejections += 1
        # The repeated error is often a delegate-pointing gate rejection —
        # count it toward the auto-delegate threshold too.
        _salvaged = False
        if "specialist.delegate" in _eid:
            rs.delegate_refusals += 1
            if (d.auto_delegate_after
                    and rs.delegate_refusals >= d.auto_delegate_after):
                _salvaged = await d.auto_delegate(
                    "repeat-error refusal streak")
        if d.guard_max and rs.guard_rejections >= d.guard_max \
                and not _salvaged:
            rs.wrap_up = True
        plan["guard_refused"] = True
        plan["result"] = ToolResult(
            status="error", result=None,
            tool_name=name,
            error=f"blocked: '{name}' with these "
                  f"arguments already failed {_cnt} "
                  f"times with the same error "
                  f"({_eid}). Repeating it will not "
                  "change the result — change the "
                  "approach or the tool.")
        await d.emit("repeat_blocked", rs.budget.iterations,
                     {"tool": name, "count": _cnt, "error": _eid})
        return True


class JspaceBadgeGate(DispatchGate):
    """j-space badge+plan gate (loop_guard.jspace_badge_gate): the skill's
    protocol order is classify → badge → plan → work, so BOTH openers must
    be in place before file work OR delegation — in a j-space run
    delegation IS the implementation lane, and an unplanned
    specialist.delegate / agent.spawn / agent.fanout moves the first edit
    into a child where this gate can't see it (live: dispatch-mode runs
    badged, then delegated the rename with no todos plan). The write test
    is _gate_write_like (audit #28 C1): fs.write/fs.edit/code.patch AND
    shell writes via code.run/code.execute/code.check (redirects, tee,
    sed -i, cp/mv…) — brains implement through heredocs when fs.* is
    closed (the delegate gate's live lesson). Once both openers land the
    gate latches open for the rest of the run. The .jspace/ ledger stays
    writable (the skill maintains it with fs.* tools); run.badge/todos/
    note.set/fs.read are never gated. The rejection feeds back as a normal
    tool error naming ONLY the missing opener(s)."""

    name = "jspace_badge_gate"
    ablatable = True

    async def check(self, plan: dict, rs) -> bool:
        d = self.dctx
        name = plan["name"]
        raw_args = plan["raw_args"]
        # The latch check runs for EVERY call, gated or not — it reads
        # pre-execution state: a same-batch [run.badge, fs.write] pair still
        # gets fs.write refused because the badge call hasn't executed yet.
        if (d.jspace_badge_gate_on
                and rs.badge_watch == "j-space"
                and not rs.jspace_gate_open):
            if rs.badged and (rs.todo_list.items
                              or rs.todo_list.requirements):
                rs.jspace_gate_open = True
        if not (d.jspace_badge_gate_on
                and rs.badge_watch == "j-space"
                and not rs.jspace_gate_open
                and ((_gate_write_like(name, raw_args)
                      and not _jspace_ledger_target(raw_args))
                     or name in _DELEGATE_TOOLS
                     or name in ("agent.spawn", "agent.fanout"))):
            return False
        rs.guard_rejections += 1
        if d.guard_max and rs.guard_rejections >= d.guard_max:
            # A brain that will not comply after max_rejections rejections
            # doesn't get to spin to the iteration cap — same endgame as
            # the other dispatch gates.
            rs.wrap_up = True
        _missing_badge = not rs.badged
        _missing_plan = not (rs.todo_list.items
                             or rs.todo_list.requirements)
        if _missing_badge and _missing_plan:
            _err = ("BLOCKED (j-space badge gate): the "
                    "j-space protocol is classify → badge → "
                    "plan → work, and neither the badge nor "
                    "the plan is in place — this call was "
                    "NOT executed. Do now: classify the task "
                    "(fast / full / loop), call `run.badge` "
                    "with label \"j-space: full\" or "
                    "\"j-space: loop\" (\"j-space: fast\" is "
                    "the honest badge for one-step work), "
                    "and set a plan with the todos tool — "
                    "then re-issue the call.")
        elif _missing_plan:
            _err = ("BLOCKED (j-space badge gate): set a "
                    "plan with the todos tool before starting "
                    "file work (j-space: classify → badge → "
                    "plan → work) — the badge is in place, "
                    "the plan is not; this call was NOT "
                    "executed. Set the plan, then re-issue "
                    "the call.")
        else:
            _err = ("BLOCKED (j-space badge gate): the "
                    "j-space protocol badges the pass BEFORE "
                    "any file work (j-space: classify → "
                    "badge → plan → work), and no run.badge "
                    "call has landed yet — this call was NOT "
                    "executed. Do now: classify the task "
                    "(fast / full / loop), call `run.badge` "
                    "with label \"j-space: full\" or "
                    "\"j-space: loop\" (\"j-space: fast\" is "
                    "the honest badge for one-step work), "
                    "then re-issue the call.")
        plan["guard_refused"] = True
        plan["result"] = ToolResult(
            status="error", result=None, tool_name=name,
            error=_err)
        await d.emit("guard_fired", rs.budget.iterations,
                     {"name": "jspace_badge_gate",
                      "phase": "dispatch",
                      "turn": rs.budget.iterations})
        return True


class StrengthGate(DispatchGate):
    """Strength gate: the request matched a routed strength domain with a
    live or swappable holder — the implementation goes through that
    specialist FIRST. Never a deadlock: one specialist.delegate call
    disarms it (sets delegated) and performs the swap if needed."""

    name = "strength_gate"
    ablatable = True

    async def check(self, plan: dict, rs) -> bool:
        d = self.dctx
        name = plan["name"]
        if not (rs.strength_gate and not rs.delegated
                and _gate_write_like(name, plan["raw_args"])):
            return False
        _gtag, _galias, _gmode = rs.strength_gate
        if _gmode == "swap":
            _ghold = (f"`{_galias}` holds that tag — "
                      "specialist.delegate swaps it onto its slot")
        elif _gmode == "allround":
            _ghold = (f"no {_gtag}-tagged preset — the "
                      f"allround specialist `{_galias}` takes it")
        else:
            _ghold = f"`{_galias}` holds that tag live"
        rs.delegate_refusals += 1
        if (d.auto_delegate_after
                and rs.delegate_refusals >= d.auto_delegate_after):
            await d.auto_delegate("strength-gate refusal streak")
        plan["result"] = ToolResult(
            status="error", result=None, tool_name=name,
            error=(f"BLOCKED — {_gtag} work routes to the "
                   f"specialist: specialist.delegate(task=…, "
                   f"strength=\"{_gtag}\"), then verify."
                   if rs.delegate_refusals > 1 else
                   f"inline implementation is closed for this "
                   f"run — this is {_gtag} work: "
                   f"call `specialist.delegate` with "
                   f"strength=\"{_gtag}\" "
                   f"({_ghold}), then verify its report"))
        return True


class DispatchModeGate(DispatchGate):
    """Dispatcher profile (brain_mode: dispatch): source-file writes are
    rejected from the FIRST call, no threshold — the brain plans/delegates/
    verifies and never authors code. Prose/config/data writes pass. One
    specialist.delegate call disarms (integration glue is a judgment call
    after the specialist reported). procedure.save is rejected outright:
    it authors .py/.sh without path args (audit 2026-10-10 — save+run was
    a full bypass: the brain could write AND execute its own code in
    dispatch mode). procedure.run stays open (verify lane, code.check
    engine)."""

    name = "dispatch_gate"
    ablatable = True

    async def check(self, plan: dict, rs) -> bool:
        d = self.dctx
        name = plan["name"]
        authors_code = ((name in ("fs.write", "fs.edit")
                         and _code_file_target(plan["raw_args"]))
                        or name == "procedure.save")
        if not (d.dispatch_gate and not rs.delegated and authors_code):
            return False
        rs.delegate_refusals += 1
        if (d.auto_delegate_after
                and rs.delegate_refusals >= d.auto_delegate_after):
            await d.auto_delegate("dispatch-gate refusal streak")
        plan["result"] = ToolResult(
            status="error", result=None, tool_name=name,
            error=("BLOCKED — source files stay closed to the "
                   "orchestrator: specialist.delegate(task=…, "
                   "strength=\"coding\"), then verify with "
                   "code.check."
                   if rs.delegate_refusals > 1 else
                   "source files are closed to the "
                   "orchestrator — hand the implementation "
                   "to `specialist.delegate` (strength="
                   "\"coding\"), then verify its report "
                   "with code.check"))
        return True


class DelegateHardGate(DispatchGate):
    """Delegate gate, hard mode: enforce mode from the config threshold;
    the brain-gate escalation from twice it. Either way the write-like call
    is rejected pre-exec — the rejection IS the message, and one
    specialist.delegate call disarms."""

    name = "delegate_hard"
    ablatable = True

    async def check(self, plan: dict, rs) -> bool:
        d = self.dctx
        name = plan["name"]
        _enforce_at = (d.delegate_after if d.delegate_enforce
                       else 2 * d.delegate_after
                       if (d.delegate_escalate and d.brain_gate) else 0)
        if not (_enforce_at and d.depth == 0
                and not rs.delegated
                and rs.inline_writes + 1 >= _enforce_at
                and _gate_write_like(name, plan["raw_args"])
                and rs.delegate_ok):
            return False
        # This write would reach the threshold — reject it so the
        # implementation goes through the specialist instead (after=1
        # blocks the very first inline write: delegate FIRST). Never a
        # deadlock: one specialist.delegate call disarms.
        rs.delegate_refusals += 1
        if (d.auto_delegate_after
                and rs.delegate_refusals >= d.auto_delegate_after):
            await d.auto_delegate("delegate-gate refusal streak")
        plan["result"] = ToolResult(
            status="error", result=None, tool_name=name,
            error=("BLOCKED — inline implementation stays "
                   "closed: specialist.delegate(task=…), "
                   "then verify its report."
                   if rs.delegate_refusals > 1 else
                   "inline implementation is closed for this "
                   "run — call `specialist.delegate` with a "
                   "complete, standalone task (the specialist "
                   "model does the heavy lifting), then "
                   "verify its report"))
        return True


class ParseArgsGate(DispatchGate):
    """JSON arg parsing — the pipeline pivot: every gate below needs a
    parsed dict. On invalid JSON, sanitize the stored assistant history in
    place (llama-server 500s re-parsing HISTORY tool calls with invalid-JSON
    arguments — live: tb-mcmc-sampling-stan died turn 3) and refuse."""

    name = "parse_args"

    async def check(self, plan: dict, rs) -> bool:  # noqa: ARG002
        name = plan["name"]
        raw_args = plan["raw_args"]
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError as e:
            # The error result below already carries the failure, so
            # replace the args with valid empty JSON in place.
            plan["fn"]["arguments"] = "{}"
            plan["result"] = ToolResult(status="error", result=None,
                                        tool_name=name,
                                        error=f"invalid JSON args: {e}")
            return True
        if args is None:
            args = {}            # no-argument call: `arguments` omitted/null
        elif not isinstance(args, dict):
            plan["result"] = ToolResult(
                status="error", result=None, tool_name=name,
                error="malformed args: tool arguments must be a JSON object")
            return True
        plan["args"] = args
        return False


class FreshRetryGate(DispatchGate):
    """Fresh-perspective retry (GVS5H §4.4): the same task cluster delegated
    again after `fresh_retry_after` failed attempts → de-anchor it: the
    child gets the RAW user request (not the brain's stuck re-framing) and
    specialist.delegate skips its orientation pack. The assistant history
    keeps the original call; the result notes the rewrite. Must run AFTER
    arg parsing and BEFORE the duplicate guard's signature computation."""

    name = "fresh_retry"
    ablatable = True

    async def check(self, plan: dict, rs) -> bool:
        d = self.dctx
        name = plan["name"]
        args = plan["args"]
        if not (d.fresh_retry_enabled and d.fresh_retry_after
                and (name in _DELEGATE_TOOLS or name == "agent.spawn")
                and isinstance(args.get("task"), str)):
            return False
        _ntok = d.runtime._arg_tokens({"task": args["task"]})
        _trial = next(
            (t for t in rs.delegate_trials
             if d.runtime._jaccard(t["tokens"], _ntok) >= 0.5),
            None)
        if (_trial is None or _trial["fresh"]
                or _trial["failures"] < d.fresh_retry_after):
            return False
        _trial["fresh"] = True
        args = dict(args)
        args["task"] = (
            "FRESH RETRY — earlier attempts at this task "
            "failed. This delegation is deliberately "
            "de-anchored: solve the ORIGINAL request below "
            "from scratch with a DIFFERENT approach. Do "
            "not read, patch, or build on files left by "
            "the earlier attempts unless you have verified "
            "they are correct.\n\nORIGINAL REQUEST:\n"
            + (d.user_message or "")[:6000])
        if name in _DELEGATE_TOOLS:
            args["fresh"] = True
        plan["args"] = args
        plan["fresh_retry"] = True
        await d.emit("fresh_retry", rs.budget.iterations,
                     {"tool": name, "failures": _trial["failures"]})
        return False


class StrengthAssistGate(DispatchGate):
    """Strength gate assist: the gate armed on THIS run's keyword match,
    but small brains drop the strength= argument the rejection directive
    told them to pass (live: 4/4 security delegates went out without it and
    silently routed coding — no swap happened). The harness already knows
    the domain; inject it so the delegate routes (and swaps) correctly. An
    explicit strength= from the model always wins."""

    name = "strength_assist"

    async def check(self, plan: dict, rs) -> bool:
        name = plan["name"]
        args = plan["args"]
        if (rs.strength_gate and name in _DELEGATE_TOOLS
                and not args.get("strength")):
            args["strength"] = rs.strength_gate[0]
        return False


class DuplicateGate(DispatchGate):
    """Duplicate guard (loop guard) — exempt poll-safe tools
    (job.status/logs/wait): repeatedly checking the same job while it runs
    is expected. Repeats count only within the current mutation generation:
    a query repeated after any successful non-read_only call may see NEW
    state, so it is never a duplicate. Exact repeats run before the
    near-duplicate guard."""

    name = "duplicate"
    ablatable = True

    async def check(self, plan: dict, rs) -> bool:
        d = self.dctx
        name = plan["name"]
        args = plan["args"]
        call_sig = d.runtime._call_signature(name, args)
        poll_exempt = name in d.runtime._poll_safe
        sig_key = (call_sig, rs.mutation_gen)
        plan["poll_exempt"] = poll_exempt
        if poll_exempt or rs.recent_calls.count(sig_key) < 2:
            if not poll_exempt:
                rs.recent_calls.append(sig_key)
                if len(rs.recent_calls) > 20:
                    rs.recent_calls.pop(0)
            return False
        rs.guard_rejections += 1
        # Escalation: enough refusals → the NEXT turn runs with tools
        # disabled (the wrap-up is announced above the model-turn call) —
        # unless the auto-delegate salvage hands the work off first. The
        # refusal stays per-call.
        if d.guard_max and rs.guard_rejections >= d.guard_max:
            await d.wrap_up_or_salvage("rejection cap (duplicates)")
        plan["guard_refused"] = True
        plan["result"] = ToolResult(
            status="error", result=None, tool_name=name,
            error=f"duplicate tool call (loop guard): '{name}' with "
                  "these exact args already ran twice and nothing it "
                  "reads has changed since — the result would be "
                  "identical. Use the earlier result and move on; "
                  "do NOT call it again with the same args.")
        return True


class NearDuplicateGate(DispatchGate):
    """Near-duplicate guard (query-like tools only): the exact check misses
    reworded repeats — the same search with shuffled/added words. Two
    similar calls are fine (refinement); the third is the overthinking
    pattern and is blocked with a synthesize-now message."""

    name = "near_duplicate"
    ablatable = True

    async def check(self, plan: dict, rs) -> bool:
        d = self.dctx
        name = plan["name"]
        args = plan["args"]
        if plan.get("poll_exempt") or name not in d.near_dup_tools \
                or not d.near_dup_threshold:
            return False
        ntok = d.runtime._arg_tokens(args)
        nflg = d.runtime._flag_sig(args)
        # Only same-lane calls compare: a different boolean flag signature
        # (plain GET vs js=true headless) is a different call, not a
        # reworded repeat.
        similar = sum(
            1 for pn, pgen, ptok, pflg in rs.recent_query_calls
            if pn == name and pgen == rs.mutation_gen
            and pflg == nflg
            and d.runtime._jaccard(ptok, ntok) >= d.near_dup_threshold)
        if similar >= 2:
            rs.guard_rejections += 1
            if d.guard_max and rs.guard_rejections >= d.guard_max:
                await d.wrap_up_or_salvage(
                    "rejection cap (near-duplicates)")
            plan["guard_refused"] = True
            plan["result"] = ToolResult(
                status="error", result=None, tool_name=name,
                error=f"near-duplicate tool call (loop guard): "
                      f"'{name}' with very similar args already "
                      "ran twice — rewording the query will not "
                      "produce new information. Synthesize your "
                      "answer from the results you already have, "
                      "or ask the user; do NOT issue another "
                      "variant of this query.")
            return True
        rs.recent_query_calls.append((name, rs.mutation_gen, ntok, nflg))
        if len(rs.recent_query_calls) > 20:
            rs.recent_query_calls.pop(0)
        return False


class PrivacyGate(DispatchGate):
    """Privacy gate: a cloud-LLM call while the conversation holds private
    tool results needs an explicit human ok — the request carries the full
    call args (the prompt), so the decision is informed. A refusal is a
    per-call error, never a run-ender: the model can fall back to a local
    tool. share_private is the blanket opt-in; auto_confirm deliberately
    does NOT waive this one. The check is target-aware: llm.call aimed at a
    local alias (incl. the vision slot for image calls) never leaves the
    box and never gates."""

    name = "privacy"

    async def check(self, plan: dict, rs) -> bool:
        d = self.dctx
        name = plan["name"]
        args = plan["args"]
        if d.share_private or not rs.private_taint \
                or not d.runtime._is_cloud_call(name, args):
            return False
        if not await d.runtime._confirm_privacy(name, args, d.run_id, d.emit,
                                                d.confirm_provider):
            plan["result"] = ToolResult(
                status="error", result=None, tool_name=name,
                error="blocked by privacy: the conversation contains "
                      "private tool results and the cloud call was not "
                      "approved. Use a local tool/model instead, or ask "
                      "the user to enable 'share with cloud' for this run.")
            return True
        plan["privacy_ok"] = True   # one prompt covered both gates
        return False


class ConfirmationGate(DispatchGate):
    """Confirmation gate: pause for human approval on tools that need it
    (job.start, git.commit, …) or that reach a cloud LLM when
    confirm_cloud_calls is on. No-op unless confirmation.enabled. Last
    gate — after it, the call is executable."""

    name = "confirmation"

    async def check(self, plan: dict, rs) -> bool:  # noqa: ARG002
        d = self.dctx
        name = plan["name"]
        args = plan["args"]
        tool_obj = d.runtime.registry.get(name)
        confirm_cloud = (d.runtime.config.get("confirmation", {}) or {}
                         ).get("confirm_cloud_calls", True)
        needs_confirm = (
            (tool_obj is not None and tool_obj.needs_confirmation(args, d.ctx))
            or (confirm_cloud and d.runtime._is_cloud_call(name, args)))
        if (needs_confirm and not plan.get("privacy_ok")
                and not await d.runtime._confirm(name, args, d.run_id,
                                                 d.auto_confirm, d.emit,
                                                 d.confirm_provider)):
            plan["result"] = ToolResult(
                status="error", result=None, tool_name=name,
                error="declined: human did not approve this tool call")
            return True
        return False


# The dispatch pipeline, in EXACT historical inline order — load-bearing:
# malformed → admin → allowlist (name exists; admin before allowlist for
# error quality); the raw-args gates (stall/repeat/jspace/strength/
# dispatch/delegate) before parsing, repeat-error before the delegate-
# pointing gates so re-issued rejections are caught; parse before the
# rewrite gates; rewrites before the duplicate signature; exact before
# near-duplicate; privacy before confirmation (privacy_ok suppresses the
# double prompt).
DISPATCH_GATES: list[type[DispatchGate]] = [
    MalformedCallGate,
    AdminOnlyGate,
    AllowlistGate,
    StallHardStopGate,
    RepeatErrorBlockGate,
    JspaceBadgeGate,
    StrengthGate,
    DispatchModeGate,
    DelegateHardGate,
    ParseArgsGate,
    FreshRetryGate,
    StrengthAssistGate,
    DuplicateGate,
    NearDuplicateGate,
    PrivacyGate,
    ConfirmationGate,
]
