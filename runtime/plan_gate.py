"""Deterministic plan-first routing for complex or risky depth-0 runs.

The legacy architect threshold is a model-rated prompt nudge. This module is
its mechanical counterpart: explicit request signals start the architect from
the harness, and a triggered run never falls through to inline implementation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PlanDecision:
    action: str = "direct"          # direct | plan | execute
    reason: str = ""
    mandatory: bool = False


_DIRECT = PlanDecision()
_READ_ONLY_PREFIXES = (
    r"^\s*(?:please\s+)?(?:explain|summari[sz]e|review|document|describe)\b",
    r"^\s*(?:please\s+)?(?:what|how)\s+(?:does|do|is|are|would|could)\b",
    r"^\s*(?:please\s+)?(?:tell\s+me\s+about|walk\s+me\s+through)\b",
)
_GENERIC_PLAN_PATTERN = r"^\s*(?:please\s+)?plan\b"
_TECHNICAL_CONTEXT = (
    r"\b(?:api|architecture|backend|bug|class|code|codebase|database|deployment|diff|"
    r"feature|frontend|function|implementation|migration|module|refactor|release|repo|"
    r"repository|scheduler|schema|script|server|service|software|tests?)\b",
)
_PLAN_PATTERNS = (
    r"\bplan(?:ning)?\s+(?:first|only)\b",
    r"\b(?:create|write|draft|produce)\s+(?:a\s+)?(?:design|implementation)\s+plan\b",
    r"\b(?:create|write|draft|produce)\s+(?:a\s+)?design\s+(?:doc|document)\b",
    r"\bdo\s+not\s+implement\b", r"\bdon['’]t\s+implement\b",
)
_COMPLEX_PATTERNS = (
    r"\b(?:implement|build|create|add|develop|code|refactor|update|modify)\b"
    r"[^.!?\n]{0,100}\bmulti[- ]file\b",
    r"\bmulti[- ]file\b[^.!?\n]{0,100}\b(?:implementation|refactor|changes?|updates?)\b",
    r"\bcodebase[- ]wide\b", r"\bcross[- ]system\b",
    r"\bnew\s+(?:system\s+)?architecture\b", r"\bdesign\s+and\s+implement\b",
    r"\b(?:implement|build|create|add|develop|code)\b"
    r"[^.!?\n]{0,100}\bnew\s+feature\b",
    r"\bnew\s+feature\b[^.!?\n]{0,100}\b(?:implementation|development)\b",
    r"\b(?:major|large|complex)\s+refactor\b",
    r"\brefactor\s+(?:the\s+)?(?:codebase|architecture|system)\b",
    r"\bambiguous\s+(?:design|scope|requirements?)\b",
    r"\b(?:complex|large)\s+(?:task|project|build)\b",
    r"\b(?:build|create)\s+(?:a\s+)?(?:full|complete)\s+(?:application|project|system)\b",
)
_MANDATORY_PATTERNS = (
    r"\bdeploy(?:ing)?\s+(?:to\s+)?production\b", r"\bforce[- ]push\b",
    r"\bdrop\s+(?:the\s+)?(?:database|table)\b",
    r"\bdelete\s+(?:the\s+)?(?:production|shared|database)\b",
    r"\b(?:run|execute|apply|perform)\s+(?:the\s+)?(?:data|schema)\s+migration\b",
)


def _hit(patterns: tuple[str, ...], message: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, message, re.I)
        if match:
            return match.group(0)
    return None


def choose_plan(user_message, *, mode: str = "auto", threshold: int = 4) -> PlanDecision:
    """Return the harness action before any model call.

    `plan` always produces a plan-only run. `direct` bypasses only discretionary
    planning; destructive/outward-facing requests still stop at a plan. In
    `auto`, threshold=0 disables discretionary automation as before.
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return _DIRECT
    if mode == "plan":
        return PlanDecision("plan", "user selected plan mode")
    if _hit(_READ_ONLY_PREFIXES, user_message):
        return _DIRECT
    risky = _hit(_MANDATORY_PATTERNS, user_message)
    if risky:
        return PlanDecision("plan", f"risky/outward-facing request: {risky}", True)
    if mode == "direct":
        return _DIRECT
    if mode != "auto" or not threshold:
        return _DIRECT
    planned = _hit(_PLAN_PATTERNS, user_message)
    if not planned and _hit(_TECHNICAL_CONTEXT, user_message):
        planned = _hit((_GENERIC_PLAN_PATTERN,), user_message)
    if planned:
        return PlanDecision("plan", f"explicit plan request: {planned}")
    complex_ = _hit(_COMPLEX_PATTERNS, user_message)
    if complex_:
        return PlanDecision("execute", f"complex implementation: {complex_}")
    return _DIRECT


def prepare_architect(runtime, rs, user_message, *, depth: int, mode: str,
                      threshold: int, disabled_tools) -> PlanDecision:
    """Choose routing and make a triggered tool available in the frozen set."""
    decision = (choose_plan(user_message, mode=mode, threshold=threshold)
                if depth == 0 else _DIRECT)
    if (decision.action != "direct" and rs.allowed is not None
            and "architect" not in rs.allowed
            and runtime.registry.get("architect") is not None
            and "architect" not in (disabled_tools or ())):
        rs.allowed.append("architect")
    return decision


async def maybe_auto_architect(runtime, decision: PlanDecision, *, user_message,
                               depth: int, ctx, rs, emit) -> bool:
    """Run a triggered architect directly; True means the parent run is done.

    There is deliberately no model-led fallback after a triggered failure:
    failing closed is what prevents the old 'ignored the architect nudge'
    behaviour from reappearing under a different name.
    """
    if depth != 0 or decision.action == "direct":
        return False
    await emit("plan_gate", 0, {
        "action": decision.action, "reason": decision.reason,
        "mandatory": decision.mandatory})
    if (runtime.registry.get("architect") is None
            or (rs.allowed is not None and "architect" not in rs.allowed)):
        rs.status = "error"
        rs.error_msg = "plan gate triggered but the architect tool is unavailable"
        rs.final_answer = f"[Run stopped: {rs.error_msg}]"
        await emit("architect_autostart", 0, {
            "action": decision.action, "status": "unavailable",
            "reason": decision.reason})
        return True
    args = {"task": user_message, "execute": decision.action == "execute",
            "review": "egm"}
    result = await runtime._execute_tool("architect", args, ctx)
    ok = result.status == "ok"
    payload = result.result if isinstance(result.result, dict) else {}
    rs.tools_used.append("architect")
    rs.trajectory.append(f"architect(auto:{decision.action})→{result.status}")
    rs.final_answer = (payload.get("answer") or payload.get("handoff")
                       or (f"[Run stopped: {result.error}]" if not ok else ""))
    rs.status = "ok" if ok else "error"
    rs.error_msg = "" if ok else (result.error or "architect failed")
    if ok:
        rs.mutation_gen += 1
    preview = result.to_model_message()
    await emit("tool_result", rs.budget.iterations, {
        "tool": "architect", "args": args, "status": result.status,
        "error": result.error, "result_preview": preview[:8000] if ok else None,
        "latency_ms": result.latency_ms, "tokens": result.tokens_used,
        "private": result.private})
    await emit("architect_autostart", rs.budget.iterations, {
        "action": decision.action, "status": result.status,
        "reason": decision.reason})
    return True
