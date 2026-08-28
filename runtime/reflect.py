"""In-chat correction capture — the "reflect" path.

JayNet's improvement loop (flag → judge → proposal → human accept) only
learns from sessions someone FLAGGED or from eval failures. An explicit
mid-chat correction in an otherwise successful session — "no, use uv
instead of pip" — taught the model for that one conversation and was then
lost. This module is the capture path for those teachings:

  user message → cheap lexical gate (is this phrased as a correction?)
  → LOCAL model verdict (is it a generalizable rule, and for which skill?)
  → dedup'd proposal in the eval proposals inbox (Admin → Eval → Proposals)

Same doctrine as the eval loop: nothing auto-applies — the admin accepts or
rejects in the inbox, and accepting a skill-tweak appends a dated bullet to
the skill's custom layer (git diff = review trail).

Privacy: the analysis call is hard-pinned to the local brain alias (chat
content never leaves the box), same posture as the eval case drafter.
Runs detached after the chat run finishes — zero latency for the user.
"""

from __future__ import annotations

import json
import logging
import re

from runtime import paths
from runtime.eval_store import EvalStore
from runtime.tool_base import ToolContext

log = logging.getLogger(__name__)

# Local-only analysis alias — never a parameter, same as eval drafting.
_ALIAS = "local-orchestrator"

# Cheap lexical pre-gate: strong correction markers only. False negatives
# are fine (a missed phrasing just isn't captured); false positives cost
# one local model call and a "not a teaching" verdict — harmless.
_CORRECTION_RE = re.compile(
    r"(\bno,\s*(use|don'?t|do\s+not|never|always|stop)\b"
    r"|\binstead\s+of\b"
    r"|\bnever\s+(use|do|forget|assume|delete|send)\b"
    r"|\balways\s+(use|check|run|remember|ask|prefer)\b"
    r"|\bstop\s+(using|doing)\b"
    r"|\bdon'?t\s+(use|forget|delete|send)\b"
    r"|\bdo\s+not\s+(use|forget|delete|send)\b"
    r"|\bfrom\s+now\s+on\b"
    # German markers (the operator mixes languages)
    r"|\bab\s+jetzt\b|\bvon\s+jetzt\s+an\b"
    r"|\bnicht\s+mehr\b|\bnie\s+wieder\b|\bniemals\b"
    r"|\bimmer\s+(verwenden|nutzen|prüfen|fragen)\b)",
    re.IGNORECASE)

_SYSTEM = """You decide whether a user message to an AI assistant is an explicit, GENERALIZABLE teaching — a correction of the assistant's behavior meant as a rule for FUTURE sessions — and if so, phrase it as an improvement proposal.

Reply with a single JSON object, no prose:
{"teaching": true|false,
 "what": "one sentence: what the user corrected",
 "cause": "why the assistant behaved wrongly (e.g. 'skill instructions lack this rule')",
 "fix": "one imperative sentence directed at the assistant, no meta-prefix like 'Add a directive'",
 "classification": "skill-tweak" | "prompt-tweak",
 "target": "name of the loaded skill this corrects, or empty string"}

Rules:
- teaching=false for: task-specific content ("the deadline is Friday"), facts about the world, one-off preferences for THIS conversation, answers to the assistant's questions, vague dissatisfaction ("that's wrong"), or anything unclear.
- teaching=true only for explicit behavior rules: "use X instead of Y", "never do X", "always check Y", "stop doing X".
- classification="skill-tweak" ONLY when the correction concerns how one of the listed loaded skills does its job — target must be one of the listed skill names. Everything else is "prompt-tweak" with an empty target.
- fix must be short, concrete and reusable across sessions."""


def _cfg(config: dict) -> dict:
    return (config or {}).get("reflect") or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", True))


def looks_like_correction(config: dict, message: str) -> bool:
    """The cheap pre-gate: enabled, short message, strong correction marker,
    not a slash command. Deliberately conservative — the model verdict below
    is the real decision."""
    if not enabled(config):
        return False
    msg = (message or "").strip()
    if not msg or msg.startswith("/"):
        return False
    cap = int(_cfg(config).get("max_message_chars") or 800)
    if len(msg) > cap:
        return False
    return bool(_CORRECTION_RE.search(msg))


def _extract_json(text: str) -> dict | None:
    """First {...} block in the model's reply, parsed. None on any drift."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except (ValueError, json.JSONDecodeError):
        return None
    return d if isinstance(d, dict) else None


async def analyze(runtime, *, message: str, answer: str,
                  skills: list[str]) -> dict | None:
    """The model verdict. Returns the validated proposal payload, or None
    when the message is not a generalizable teaching (or the model drifted).
    LOCAL alias only — the payload carries private chat content."""
    from tools.llm.cloud_models import _call_via_litellm
    payload = (
        f"Loaded skills this session: {', '.join(skills) or '(none)'}\n\n"
        f"## Assistant answer being corrected (excerpt)\n"
        f"{(answer or '')[:1200]}\n\n"
        f"## User message\n{message.strip()[:1200]}")
    ctx = ToolContext(request_id="reflect", config=runtime.config, budget=None)
    res = await _call_via_litellm(_ALIAS, payload, None, _SYSTEM, False,
                                  None, ctx)
    if res.status != "ok":
        log.info("reflect: analysis call failed: %s", res.error)
        return None
    d = _extract_json(str(res.result or ""))
    if not d or d.get("teaching") is not True:
        return None
    fix = str(d.get("fix") or "").strip()
    what = str(d.get("what") or "").strip()
    if not fix or not what:
        return None
    cls = str(d.get("classification") or "").strip()
    target = str(d.get("target") or "").strip()
    if cls == "skill-tweak" and target not in skills:
        cls, target = "prompt-tweak", ""     # hallucinated skill → downgrade
    if cls not in ("skill-tweak", "prompt-tweak"):
        cls, target = "prompt-tweak", ""
    return {"classification": cls, "target": target or None,
            "what": what, "cause": str(d.get("cause") or "unclear").strip(),
            "fix": fix}


async def maybe_capture(runtime, *, message: str, answer: str,
                        run_ids: list[str], owner: str | None) -> dict | None:
    """Full path: gate → loaded-skill lookup (trace) → model verdict →
    dedup'd proposal in the inbox. Returns the proposal row (None when the
    message taught nothing new, or an identical proposal is already
    pending/accepted). Never raises — capture must never break a chat."""
    try:
        if not looks_like_correction(runtime.config, message):
            return None
        from runtime import eval_runner  # late: heavy import graph
        skills = sorted(eval_runner._skill_loads_from_trace(run_ids or []))
        payload = await analyze(runtime, message=message, answer=answer,
                                skills=skills)
        if payload is None:
            return None
        store = EvalStore(paths.EVAL_DB)
        try:
            return store.add_proposal(
                test_id=f"reflect:{owner or 'token'}", result_id=None,
                classification=payload["classification"],
                target=payload["target"], proposed_content=None,
                what=payload["what"], cause=payload["cause"],
                fix=payload["fix"])
        finally:
            store.close()
    except Exception as e:
        log.info("reflect: capture failed (ignored): %s", e)
        return None
