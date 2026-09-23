"""Final-answer guards (code audit 2026-09-23, P2 step 2 + item 7).

When the model returns TEXT instead of tool calls, a chain of bounce-style
guards decides whether the answer is accepted. Historically this was one
if-chain inside AgentRuntime.run; each guard shared one shape: check a
condition → one-shot flag → emit an event → append a nudge message →
continue. They are now registered classes; the loop (runtime/loop.py)
iterates FINAL_ANSWER_GUARDS and applies the first Nudge returned.

ORDER IS LOAD-BEARING — it is the historical order of the inline if-chain,
kept exactly so event ordering in traces, the web UI and live eval analysis
is unchanged:

 1. cap             empty answer, finish 'length' (thinking ate the cap)
 2. trunc           non-empty answer cut at the completion cap mid-sentence
 3. empty           empty answer, finish != 'length'
 4. markup          leaked tool-call markup as the answer
 5. requirements    open [must] requirements/todos
    ── the loop pins rs.final_answer to the candidate answer here
       (DeliverableGuard.pin_answer), the legacy pin point between the
       requirements and deliverable checks — it happens even when the
       deliverable guard is disabled.
 6. deliverable     task-named files missing from the workspace
 7. verify_delegate delegated implementation, no check tool after it
 8. just_reply      compute/fresh-data request, zero tool calls in the run
 9. procedure       auto-loaded procedure checkpoints unconfirmed

This is already roughly value-ordered for the bounce cap (audit item 7:
"order the guards by value — the current order is already roughly that;
keep it"): the answer-shape guards fire first (a missing or garbled answer
is the most valuable bounce), the work-verification bounces next, the
checklist reminder last. No deviation from the historical order.

Bounce cap (item 7): every bounce costs a full model turn over a growing
context — on a ~30 tok/s brain the historical worst case (8+ bounces on one
answer) meant minutes of stall. The loop caps the bounces ONE final answer
may earn at agent.max_bounces_per_answer (default 3, 0 disables); at the
cap the answer is accepted and a `bounce_cap` event names the guard that
would have fired. Counting is PER ANSWER: a turn with tool calls ends the
answer sequence and resets the count (the loop owns the counter).

Contract: check() is side-effect-free — it inspects and returns a Nudge or
None. The loop sets guard.fired when it APPLIES a nudge, so a nudge
suppressed by the bounce cap leaves the guard unfired (it may legitimately
fire on a later answer). Event names and payload keys are identical to the
inline era on purpose — traces and the UI read them.

The verifier gate (agent `verify=` runs) is deliberately NOT here: it is
not a one-shot bounce but a bounded retry loop with its own stall breaker
and break semantics — it stays inline in loop.py (audit step 3+).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .model_client import _is_local_model
from .run_state import RunState

# Chat-template tool-call markup that survived parsing and leaked into the
# final answer (live: gaia-cca530fc ended 'ok' with the literal answer
# "</ifm|tool_call>\n</ifm|tool_calls>"). Covers the ifm| variant seen on
# fine-tuned templates plus the common <tool_call>/</tool_call> markers.
_MARKUP_LEAK_RE = re.compile(
    r"</?ifm\|tool_calls?\s*/?>|</?tool_calls?\s*/?>|<\|tool_calls?[^>]*>",
    re.IGNORECASE)


def _markup_leaked(answer: str) -> bool:
    """True when the final answer is ESSENTIALLY leaked tool-call markup —
    the markers present and, once stripped, almost no real text left. Prose
    that merely discusses the markers (chat-template work does) is fine."""
    if not _MARKUP_LEAK_RE.search(answer or ""):
        return False
    return len(_MARKUP_LEAK_RE.sub("", answer).strip()) < 40


@dataclass
class Nudge:
    """One bounce: the loop emits `event` with `data`, appends `message` as
    a user turn and restarts. think_off retries the next model turn with
    thinking disabled (the cap/empty guards' think-ate-the-budget cases)."""
    event: str
    data: dict
    message: str
    think_off: bool = False


@dataclass
class GuardContext:
    """Everything a final-answer guard needs beyond RunState: per-run
    plumbing (fixed when the loop builds the guard chain) plus per-turn
    data (turn, call_think — the loop refreshes both before each pass)."""
    runtime: Any            # AgentRuntime — Any: loop.py imports this module
    ctx: Any                # the run's ToolContext
    cfg: dict               # the agent: config section
    user_message: str
    depth: int
    eff_model: str
    turn: dict = field(default_factory=dict)
    call_think: bool = True


class FinalAnswerGuard:
    """Base + contract for one final-answer guard. `name` is the stable id
    (events, config, the bounce_cap payload). enabled() reads the guard's
    own config key from the agent: section; one-shot-ness is the loop-set
    `fired` flag (see the module docstring for the suppression semantics)."""
    name: str = ""
    #: pin rs.final_answer to the candidate answer BEFORE this guard's
    #: check runs (see the module docstring for the legacy pin point).
    pin_answer: bool = False

    def __init__(self, gctx: GuardContext) -> None:
        self.gctx = gctx
        self.fired = False
        self.on = self.enabled(gctx.cfg)

    def enabled(self, cfg: dict) -> bool:
        return True

    async def check(self, rs: RunState, answer: str) -> Nudge | None:
        if self.fired or not self.on:
            return None
        return await self._check(rs, answer)

    async def _check(self, rs: RunState, answer: str) -> Nudge | None:
        raise NotImplementedError


class CompletionCapGuard(FinalAnswerGuard):
    """A generation cut at the completion cap DURING REASONING comes back
    finish_reason 'length' with no content at all (thinking ate the whole
    budget — seen live: tb-regex-log ended 'ok' with an empty answer after
    8192 tokens of pure thinking). Nudge once for a brief direct reply
    instead of ending the run empty-handed."""
    name = "cap"

    async def _check(self, rs: RunState, answer: str) -> Nudge | None:
        if answer.strip() or self.gctx.turn.get("finish_reason") != "length":
            return None
        # Retry with thinking OFF when the backend honors the jinja switch:
        # the alternative (think again) just re-burns the cap. Cloud/other
        # backends keep the plain nudge — they run at provider default anyway.
        rt = self.gctx.runtime
        model = self.gctx.eff_model
        switchable = self.gctx.call_think and _is_local_model(
            model, rt._local_aliases) and (
            getattr(rt, "_think_switch_aliases", None) is None
            or model in rt._think_switch_aliases)
        message = ("Your previous reply was cut off at the completion-"
                   "token cap during reasoning and contained no "
                   "answer. Reply now — briefly and directly, no "
                   "tool calls.")
        # Replay the tail of the cut chain-of-thought so the model CONTINUES
        # from where it broke off instead of re-deriving the same chain
        # (and capping again).
        tail = (self.gctx.turn.get("reasoning_tail") or "").strip()
        if tail:
            message += ("\n\nYour reasoning was cut off; it "
                        "ended with:\n…" + tail[-900:] +
                        "\nDo not restart — conclude now.")
        return Nudge("model_turn_capped",
                     {"model": model, "think_off": switchable,
                      "completion_tokens": (self.gctx.turn.get("usage") or {})
                      .get("completion_tokens")},
                     message, think_off=switchable)


class TruncationGuard(FinalAnswerGuard):
    """NON-empty answer cut at the completion cap (the new signature with
    reasoning_budget_tokens on: visible rambling instead of an empty turn)
    — a half-sentence is not an answer. Nudge once for a concise restate."""
    name = "trunc"

    async def _check(self, rs: RunState, answer: str) -> Nudge | None:
        if not answer.strip() \
                or self.gctx.turn.get("finish_reason") != "length":
            return None
        return Nudge("model_turn_truncated",
                     {"model": self.gctx.eff_model,
                      "completion_tokens": (self.gctx.turn.get("usage") or {})
                      .get("completion_tokens")},
                     "Your previous reply was cut off at the "
                     "completion-token cap mid-sentence. Restate your "
                     "final answer concisely — a few sentences, no "
                     "tool calls.")


class EmptyFinalGuard(FinalAnswerGuard):
    """Empty final answer with finish 'stop'. Two signatures: (a) a
    thinking-only turn that stopped cleanly — bounce once for a restate;
    (b) completion_tokens hit the reasoning budget exactly (live:
    gaia-4b6bb5f7 — two turns at 4097 = budget 4096+1, zero content both
    times): the think block ate the whole turn, and a same-setup retry
    deterministically reproduces the empty turn, so the retry runs with
    thinking OFF and is told to answer from what it has. finish 'length'
    stays with the cap guard (its second empty turn ends the run)."""
    name = "empty"

    async def _check(self, rs: RunState, answer: str) -> Nudge | None:
        if answer.strip() or self.gctx.turn.get("finish_reason") == "length":
            return None
        try:
            rb = int((self.gctx.runtime.config.get("orchestrator") or {})
                     .get("reasoning_budget_tokens", 0) or 0)
        except (TypeError, ValueError):
            rb = 0
        used = int((self.gctx.turn.get("usage") or {})
                   .get("completion_tokens") or 0)
        think_ate = bool(rb) and used >= rb
        if think_ate:
            message = ("Your previous turn spent the entire reasoning "
                       "budget on thinking and contained no answer "
                       "text. Answer now from what you already have — "
                       "briefly and directly, no tool calls.")
        else:
            message = ("Your previous reply contained no answer text at "
                       "all. Restate your final answer now — briefly and "
                       "directly.")
        return Nudge("empty_final",
                     {"model": self.gctx.eff_model,
                      "finish_reason": self.gctx.turn.get("finish_reason"),
                      "reasoning_exhausted": think_ate},
                     message, think_off=think_ate)


class MarkupLeakGuard(FinalAnswerGuard):
    """Leaked tool-call markup as the "answer": template artifacts that
    survived parsing — not empty, so the empty-final bounce never fired.
    Bounce once for plain text."""
    name = "markup"

    async def _check(self, rs: RunState, answer: str) -> Nudge | None:
        if not _markup_leaked(answer):
            return None
        return Nudge("markup_leak", {"model": self.gctx.eff_model},
                     "Your previous reply was leaked tool-call markup, "
                     "not an answer. Restate your final answer in plain "
                     "text now — briefly and directly, no markup, no "
                     "tool calls.")


class RequirementsGuard(FinalAnswerGuard):
    """Requirements gate: a final answer while [must]-tagged requirements
    (or [must] todos) are still open means an explicit output requirement
    (format, spelling, "deliver as X") was never verified. Bounce once —
    the model satisfies/verifies and closes them, then answers.
    Verified/removed items don't block; a wrong-but-closed item is the
    model's call, the gate only catches "never checked"."""
    name = "requirements"

    async def _check(self, rs: RunState, answer: str) -> Nudge | None:
        open_must = [t["title"] for t in rs.todo_list.items
                     if t.get("status") in ("pending", "working")
                     and str(t.get("title") or "")
                     .lower().startswith("[must]")]
        open_must += [r for r in rs.todo_list.requirements
                      if r.lower().startswith("[must]")]
        if not open_must:
            return None
        return Nudge("requirements_gate", {"open": open_must}, (
            "Requirements check: these [must] requirements "
            "are still open:\n- " + "\n- ".join(open_must) +
            "\nVerify each against your answer — satisfy "
            "it, then drop it from the requirements list — "
            "then give your final answer."))


class DeliverableGuard(FinalAnswerGuard):
    """Deliverable check (agent.deliverable_check): named-but-missing files
    → nudge back once instead of accepting an answer that never delivered."""
    name = "deliverable"
    pin_answer = True

    def enabled(self, cfg: dict) -> bool:
        return bool((cfg.get("deliverable_check") or {}).get("enabled", True))

    async def _check(self, rs: RunState, answer: str) -> Nudge | None:
        missing = self.gctx.runtime._missing_deliverables(
            self.gctx.ctx, self.gctx.user_message, answer)
        if not missing:
            return None
        return Nudge("deliverable_check", {"missing": missing}, (
            "Deliverable check: the task named these files "
            "but they do not exist in your workspace: "
            + ", ".join(missing) + ". If any is a required "
            "deliverable, create it now with fs.write "
            "(large files: several smaller writes), verify "
            "with fs.list, then give your final answer. If "
            "none is a deliverable, say so and finish."))


class VerifyDelegateGuard(FinalAnswerGuard):
    """Verify-the-delegate bounce (agent.verify_delegate_check): a run that
    delegated implementation but ran NO check tool after the last
    delegation gets its final answer bounced once — the specialist's
    report is unverified until proven (live: tb-regex-log delegated twice
    and shipped a regex matching 1/9 dates; code-bugfix checked BEFORE the
    fix, never after). One-shot; stating why no check applies is an
    acceptable answer."""
    name = "verify_delegate"

    def enabled(self, cfg: dict) -> bool:
        return bool(cfg.get("verify_delegate_check", True))

    async def _check(self, rs: RunState, answer: str) -> Nudge | None:
        if self.gctx.depth != 0 or rs.delegate_turn < 0 \
                or rs.check_turn >= rs.delegate_turn:
            return None
        return Nudge("verify_check", {"delegate_turn": rs.delegate_turn}, (
            "Verification check: you delegated implementation "
            "to the specialist but never verified what came "
            "back — no check ran after it returned. Run "
            "code.check now (the tests/build, or a concrete "
            "probe of the deliverable), or state briefly why "
            "no check applies — then give your final "
            "answer."))


class JustReplyGuard(FinalAnswerGuard):
    """Just-reply bounce (agent.just_reply_check): compute/fresh-data
    markers in the request + a final answer with ZERO tool calls in the
    run → bounce once (live: just-replied "12000" for a computed 16000,
    multi-hop answers from memory). One-shot; a stated "no tool applies"
    clears it. The arming (keywords, depth) is computed at run start and
    lives in rs.just_reply_armed."""
    name = "just_reply"

    def enabled(self, cfg: dict) -> bool:
        return bool(cfg.get("just_reply_check", True))

    async def _check(self, rs: RunState, answer: str) -> Nudge | None:
        if not rs.just_reply_armed or rs.any_tool_turn >= 0:
            return None
        return Nudge("just_reply_check", {}, (
            "Just-reply check: you are about to answer without "
            "having used a single tool, and the request asks "
            "for a computation, exact count, decode, or current "
            "data. Do the work first — code.run the "
            "calculation/string operation, web.search the "
            "fresh fact — then give your final answer. If the "
            "question genuinely needs no tool (stable common "
            "knowledge), state that briefly and answer."))


class ProcedureGuard(FinalAnswerGuard):
    """Procedure checkpoint check: with an auto-loaded procedure, nudge
    once against ITS checklist before accepting the answer — the
    task-shaped peer of the deliverable check."""
    name = "procedure"

    async def _check(self, rs: RunState, answer: str) -> Nudge | None:
        if not rs.proc_checkpoints:
            return None
        cps = "\n".join(f"{i}. {c}" for i, c in
                        enumerate(rs.proc_checkpoints, 1))
        return Nudge("procedure_check",
                     {"skill": rs.proc_name,
                      "checkpoints": rs.proc_checkpoints}, (
            f"Procedure check ({rs.proc_name}): before finishing, "
            "go through its checklist:\n" + cps + "\nIf any "
            "item is not done yet, do it now (or state briefly "
            "why it does not apply here), then give your final "
            "answer."))


#: The final-answer guard chain, in firing order — see the module docstring.
#: ORDER IS LOAD-BEARING; append new guards, never reorder silently.
FINAL_ANSWER_GUARDS: list[type[FinalAnswerGuard]] = [
    CompletionCapGuard,
    TruncationGuard,
    EmptyFinalGuard,
    MarkupLeakGuard,
    RequirementsGuard,
    DeliverableGuard,
    VerifyDelegateGuard,
    JustReplyGuard,
    ProcedureGuard,
]
