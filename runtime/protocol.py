"""Protocol v1 — the typed client/run event registry (harness lessons §2, stage 1).

Every event that can reach a client (SSE /api/stream, /api/v1/runs/{id}/events)
travels in one envelope shape:

    {"v": 1, "run_id", "seq", "ts", "type", "iteration", "data"}

built ONLY by build_envelope() below — the loop's emit() seam and the web
fast-paths share it, so the wire shape can never drift between producers.

EVENTS is the registry: one EventSpec per emitted type with its required and
known-optional `data` fields. It exists to TYPE the stream, not to constrain
it — the stable contract (docs/api.md) is additive, so unknown extra fields
always validate, and unknown TYPES are the only hard error. Payloads that are
genuinely free-form (progress, route_decision) are declared free_form rather
than pinned to a guessed shape that the next emit site would silently break.

Enforcement posture: check() NEVER rejects at runtime in production — with
STRICT=False (the default) a problem logs a warning once per distinct
(type, problem) and the event still flows (the consumers — web UI, Android —
must not lose events over a schema nit). STRICT=True (tests, CI meta-guard)
raises ValueError instead, which is how the suite proves a fake-model run
emits only registered, valid events.

The `token` type is flagged trace=False: streamed deltas bypass the trace on
purpose (volume — loop.py emit_token), everything else is flight-recordered.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

#: Flips check() from warn-and-pass to raise. Module-level (not env) so tests
#: toggle it with monkeypatch; production stays False — see module docstring.
STRICT = False

_NoneT = type(None)
_Num = (int, float)          # JSON number (bool excluded by _is_type)
_StrOpt = (str, _NoneT)


@dataclass(frozen=True)
class EventSpec:
    """One event type's data-payload shape.

    required: field -> accepted python types; must be present and typed.
    optional: field -> accepted types when present; absence is fine.
    free_form: payload is loose by design — required still applies, but the
        spec makes no claim beyond it (progress is the canonical case).
    trace: False = bypasses trace.log by design (token; see loop.py).
    doc: one line for the registry table in docs/api.md.
    """
    required: dict[str, tuple[type, ...]] = field(default_factory=dict)
    optional: dict[str, tuple[type, ...]] = field(default_factory=dict)
    free_form: bool = False
    trace: bool = True
    doc: str = ""


def _s(*fields: str) -> dict[str, tuple[type, ...]]:
    """Shorthand: required string fields."""
    return {f: (str,) for f in fields}


# The census behind this table: every `emit(` site in runtime/ (loop.py,
# dispatch_guards.py, turn_guards.py, final_guards.py, confirm.py,
# spawn_service.py, subcall.py), tools/ (ctx.emit seam), and the web
# fast-path emitters in web/routes_run.py. Types are what those sites
# ACTUALLY send (nullable where the producer can pass None) — a stricter
# spec than reality would fire warnings on healthy runs.
EVENTS: dict[str, EventSpec] = {
    # ---- run lifecycle ----
    "run_start": EventSpec(
        required=_s("message"),
        optional={"share_private": (bool,)},
        doc="run began; the user message"),
    "run_finish": EventSpec(
        required={"status": (str,), "answer": _StrOpt},
        optional={"error": _StrOpt, "budget": (dict,), "trajectory": (str,),
                  "guard_rejections": (int,), "overthinking_markers": (int,),
                  "prompt_tokens": (int, _NoneT), "context_tokens": (int, _NoneT),
                  "open_must": (list,),
                  # web fast-paths (quick-reply/slash/compact) shape:
                  "iterations": (int,), "cost_usd": _Num,
                  "total_tokens": (int,), "latency_ms": (int,),
                  "compact": (dict,)},
        doc="terminal event — status, answer, budget; closes the SSE stream"),
    # ---- model traffic ----
    "model_start": EventSpec(
        required={"model": (str,), "stream": (bool,)},
        doc="a model call is starting (UI prefill indicator)"),
    "model_turn": EventSpec(
        required={"model": (str,), "content": _StrOpt, "tool_calls": (list,)},
        optional={"served_model": (str,), "usage": (dict,),
                  "cache_hit_ratio": _Num + (_NoneT,), "ttft_s": _Num + (_NoneT,),
                  "turn_s": _Num + (_NoneT,), "content_len": (int,)},
        doc="one completed model turn: content, tool calls, usage/cache telemetry"),
    "token": EventSpec(
        required={"scope": (str,), "text": (str,)},
        optional={"model": _StrOpt},
        trace=False,
        doc="streamed text delta (scope brain|reasoning|<tool>); NOT traced"),
    "cost": EventSpec(
        required={"model": (str,), "delta_usd": _Num, "total_usd": _Num,
                  "total_tokens": (int,), "tokens_prompt": (int,),
                  "tokens_completion": (int,), "tokens_cached": (int,)},
        doc="running cost meter after each usage charge"),
    # ---- tools ----
    "tool_selection": EventSpec(
        required={"mode": (str,)},
        optional={"requested": (list, _NoneT), "selected": (list, str),
                  "available": (list,), "count": (int,), "added": (list,),
                  "diag": (dict, _NoneT)},
        doc="the frozen toolset for the run (also tools.load expansions)"),
    "tool_result": EventSpec(
        required={"tool": (str,), "status": (str,)},
        optional={"args": (dict, str, _NoneT), "error": _StrOpt,
                  "result_preview": _StrOpt, "latency_ms": _Num + (_NoneT,),
                  "tokens": (dict, _NoneT), "private": (bool,)},
        doc="one executed tool call: status, preview, latency"),
    "output": EventSpec(
        required=_s("run_id", "name", "kind") | {"size": (int,)},
        doc="deliver.files / browser surfaced a downloadable artifact"),
    # ---- sub-agents ----
    "subagent_start": EventSpec(
        required=_s("name", "model", "task") | {"depth": (int,)},
        optional={"tools": (list, _NoneT)},
        doc="a child run was spawned"),
    "subagent_finish": EventSpec(
        required={"name": (str,), "depth": (int,), "status": _StrOpt,
                  "sub_run_id": _StrOpt, "budget": (dict,)},
        doc="a child run returned"),
    "subcall": EventSpec(
        required={"model": (str,), "prompt_chars": (int,),
                  "completion_chars": (int,), "usage": (dict,),
                  "latency_ms": (int,), "call": (int,), "calls_max": (int,)},
        doc="one mediated sub-LLM call from inside code.run (RLM primitive)"),
    "auto_delegate": EventSpec(
        required=_s("reason", "strength", "tool", "status"),
        optional={"mode": _StrOpt},
        doc="the loop guard delegated harness-side after a stall"),
    # ---- human-in-the-loop ----
    "confirmation": EventSpec(
        required={"tool": (str,), "approved": (bool,), "via": (str,)},
        doc="a confirmation was decided (approved or not, by which path)"),
    "confirmation_request": EventSpec(
        required=_s("confirmation_id", "tool") | {"args": (dict,),
                                                  "timeout_s": _Num},
        optional={"reason": (str,)},
        doc="agent asks approval → resolve via /api/approve"),
    "questions_request": EventSpec(
        required={"ask_id": (str,), "questions": (list,), "timeout_s": _Num},
        doc="ask.user questions → resolve via /api/answer"),
    # ---- progress / planning ----
    "progress": EventSpec(
        optional={"label": (str,), "type": (str,), "ok": (bool,)},
        free_form=True,
        doc="loose UI progress line; only `label` is a de-facto convention"),
    "todos": EventSpec(
        required={"items": (list,)},
        optional={"requirements": (list,), "req_checks": (dict,)},
        doc="todo-list / requirements snapshot"),
    "ledger_check": EventSpec(
        required={"kind": (str,), "command": (str,), "verified": (bool,)},
        optional={"title": _StrOpt, "exit_code": (int, _NoneT), "note": (str,)},
        doc="a declared machine check ran (acceptance ledger)"),
    "badge": EventSpec(
        required=_s("label"),
        doc="skill badge ceremony marker (j-space gate)"),
    "compaction": EventSpec(
        required={"compacted": (int,)},
        optional={"deduplicated": (int,), "log_pruned": (int,),
                  "saved_chars": (int,), "estimated_saved_tokens": (int,),
                  "emergency": (bool,)},
        doc="transcript compaction count, strategies and estimated savings"),
    # ---- routing / setup ----
    "route_decision": EventSpec(
        free_form=True,
        doc="routing telemetry: source/tag/confidence/keyword-agreement (free-form)"),
    "procedure_autoload": EventSpec(
        required=_s("skill"),
        doc="a procedure was auto-loaded for this request"),
    "strength_gate": EventSpec(
        required=_s("tag"),
        optional={"mode": _StrOpt, "alias": _StrOpt, "preset": _StrOpt},
        doc="a strength-tag route armed the delegation gate"),
    # ---- guards / nudges (one event per fired rail) ----
    "guard_fired": EventSpec(
        required=_s("name", "phase") | {"turn": (int,)},
        doc="uniform per-guard telemetry (alongside the guard's own event)"),
    "budget_warning": EventSpec(
        required={"pressure": _Num, "dimension": (str,), "elapsed_s": _Num},
        doc="nearing a budget ceiling (one-shot, plus final notice)"),
    "context_warning": EventSpec(
        required={"pressure": _Num, "context_tokens": (int,),
                  "prompt_tokens": (int,)},
        doc="the prompt itself nears the context window"),
    "stall_check": EventSpec(
        required={"rung": (int,), "turns": (int,)},
        doc="stall ladder rung fired"),
    "stall_hard_stop": EventSpec(
        required={"armed": (bool,), "turns": (int,)},
        doc="final stall rung armed the tool-call stop"),
    "deliverable_warn": EventSpec(
        required={"missing": (list,), "remaining": (int,)},
        doc="early warning: task-named files not yet written"),
    "repeat_blocked": EventSpec(
        required={"tool": (str,), "count": (int,), "error": (str,)},
        doc="repeat-error hard block refused a call"),
    "fresh_retry": EventSpec(
        required={"tool": (str,), "failures": (int,)},
        doc="a delegation was de-anchored and retried fresh"),
    "bounce_cap": EventSpec(
        required={"guard": (str,), "bounces": (int,), "cap": (int,),
                  "fallback": (bool,)},
        doc="final-answer bounce cap reached; answer accepted"),
    "requirements_gate": EventSpec(
        required={"open": (list,)},
        doc="final answer bounced: open [must] requirements"),
    "deliverable_check": EventSpec(
        required={"missing": (list,)},
        doc="final answer bounced: named files missing"),
    "verify_check": EventSpec(
        required={"delegate_turn": (int,)},
        doc="final answer bounced: delegated work never verified"),
    "just_reply_check": EventSpec(
        doc="final answer bounced: zero tool calls on a compute-shaped ask"),
    "procedure_check": EventSpec(
        required={"skill": _StrOpt, "checkpoints": (list,)},
        doc="final answer bounced: procedure checklist not cleared"),
    "markup_leak": EventSpec(
        required=_s("model"),
        doc="final answer bounced: leaked tool-call markup"),
    "empty_final": EventSpec(
        required={"model": (str,), "finish_reason": _StrOpt,
                  "reasoning_exhausted": (bool,)},
        doc="final answer bounced: empty turn"),
    "model_turn_capped": EventSpec(
        required={"model": (str,), "think_off": (bool,)},
        optional={"completion_tokens": (int, _NoneT)},
        doc="final answer bounced: reasoning hit the completion cap"),
    "model_turn_truncated": EventSpec(
        required=_s("model"),
        optional={"completion_tokens": (int, _NoneT)},
        doc="final answer bounced: answer cut mid-sentence at the cap"),
    # ---- verify gate ----
    "verify": EventSpec(
        required={"ok": (bool,), "attempt": (int,),
                  "command": (str,), "report": (str,)},
        doc="verifier check result for a run with a verify spec"),
    "verify_giveup": EventSpec(
        required={"stuck": (bool,), "attempts": (int,)},
        doc="verifier never passed; run ends unverified"),
}


def _is_type(value, types: tuple[type, ...]) -> bool:
    """JSON-realistic isinstance: ints are numbers, but bools are neither
    int nor float here (a True where a count belongs IS a schema bug)."""
    for t in types:
        if t is bool:
            if isinstance(value, bool):
                return True
        elif t is int:
            if isinstance(value, int) and not isinstance(value, bool):
                return True
        elif t is float:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return True
        elif isinstance(value, t):
            return True
    return False


def _type_names(types: tuple[type, ...]) -> str:
    return "|".join("null" if t is _NoneT else t.__name__ for t in types)


def validate_event(event_type: str, data) -> list[str]:
    """Validate one event payload against the registry. Returns problem
    strings ([] = valid). Extra unknown fields are NOT problems — the
    contract is additive (docs/api.md); unknown TYPES are."""
    spec = EVENTS.get(event_type)
    if spec is None:
        return [f"{event_type!r}: unknown event type"]
    if not isinstance(data, dict):
        return [f"{event_type}: data must be a dict, got {type(data).__name__}"]
    problems: list[str] = []
    for f, types in spec.required.items():
        if f not in data:
            problems.append(f"{event_type}: missing required field {f!r}")
        elif not _is_type(data[f], types):
            problems.append(f"{event_type}: field {f!r} must be "
                            f"{_type_names(types)}, got {type(data[f]).__name__}")
    for f, types in spec.optional.items():
        if f in data and not _is_type(data[f], types):
            problems.append(f"{event_type}: field {f!r} must be "
                            f"{_type_names(types)}, got {type(data[f]).__name__}")
    return problems


# Warn-once dedup: a systematically off-schema emit site would otherwise
# log once per EVENT for the whole process lifetime.
_warned: set[tuple[str, str]] = set()


def check(event_type: str, data) -> list[str]:
    """The seam call: validate, then enforce the STRICT posture (see module
    docstring). Returns the problem list so tests can assert on it."""
    problems = validate_event(event_type, data)
    if not problems:
        return problems
    if STRICT:
        raise ValueError("protocol v1 violation: " + "; ".join(problems))
    for p in problems:
        if (event_type, p) not in _warned:
            _warned.add((event_type, p))
            log.warning("protocol v1: %s (event passed through)", p)
    return problems


def build_envelope(run_id: str, seq, event_type: str, iteration,
                   data: dict) -> dict:
    """THE envelope builder — the single source of the wire shape. Key order
    matches the historical literals so existing byte-comparing consumers
    (and the SSE `id` field) see no change."""
    return {"v": PROTOCOL_VERSION, "run_id": run_id, "seq": seq,
            "ts": time.time(), "type": event_type,
            "iteration": iteration, "data": data}
