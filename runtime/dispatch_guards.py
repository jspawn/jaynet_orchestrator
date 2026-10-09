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

