"""clm.decide / clm.rank — contrastive System One decisions via clm-serve.

The brain can consult the decision model directly: choice (pick from
candidates with probabilities), noul (calibrated yes/no), score (ordered
levels), or rank (order free-form candidates). One embedding + a dot
product per candidate, no text generation, nothing leaves the box (the
server is local; see plugins/clm/README.md for setup).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from runtime.tool_base import Tool, ToolResult


def _load_client():
    """Shared clm_client module — single sys.modules entry (see
    handoffs/plugins.md: fresh execs split module state)."""
    name = "clm_plugin_client"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parent.parent / "clm_client.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


class ClmDecide(Tool):
    name = "clm.decide"
    description = (
        "Ask the local CLM decision model: give it a `state` (the text to "
        "judge) and typed `questions` — it answers with calibrated "
        "PROBABILITIES (one embedding + a dot product per option, no "
        "generated text). Question types: choice (criteria = {candidate: "
        "description} → probability per candidate + top choice), noul "
        "(yes/no probability), score (criteria = ordered level descriptions "
        "→ expected level). Use it for routing, triage, or any "
        "classification you want a confidence number on — much cheaper and "
        "more honest than asking an LLM to guess JSON. Requires the "
        "clm-serve sidecar (plugins/clm/README.md); returns a clear error "
        "when it is down.")
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "description": "The context to judge (the document, request, "
                               "or situation).",
            },
            "questions": {
                "type": "object",
                "description": (
                    "Map of question ID → definition: {type: "
                    "'choice'|'noul'|'score', instructions: str, criteria: "
                    "{name: description} for choice, [level descriptions] "
                    "for score, optional {true:…, false:…} for noul}."),
            },
        },
        "required": ["state", "questions"],
    }

    async def execute(self, args: dict, ctx) -> ToolResult:
        client = _load_client()
        state = str(args.get("state") or "").strip()
        questions = args.get("questions")
        if not state:
            return ToolResult(status="error", result=None,
                              error="state is required")
        if not isinstance(questions, dict) or not questions:
            return ToolResult(status="error", result=None,
                              error="questions must be a non-empty object")
        try:
            answers = await asyncio.to_thread(
                client.decide, ctx.config, state, questions,
                client.settings(ctx.config)["timeout_s"])
        except client.ClmError as e:
            return ToolResult(status="error", result=None, error=str(e))
        return ToolResult(status="ok", result={"answers": answers})


class ClmRank(Tool):
    name = "clm.rank"
    description = (
        "Rank free-form candidates against a state with the local CLM "
        "decision model — best-of-N solutions, tool names, next moves, "
        "draft answers. Give `context` (the situation), `question` (what "
        "makes a candidate good) and `answers` (the candidates); get them "
        "back ordered with probabilities, best first. Cheap: one embedding "
        "for the state, a dot product per candidate. Use it to pick the "
        "best of several attempts instead of trusting the first one. "
        "Requires the clm-serve sidecar (plugins/clm/README.md).")
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "context": {
                "type": "string",
                "description": "The situation the candidates address.",
            },
            "question": {
                "type": "string",
                "description": "What makes a candidate good (the ranking "
                               "criterion, e.g. 'Which solution is correct "
                               "and complete?').",
            },
            "answers": {
                "type": "array", "items": {"type": "string"},
                "description": "The candidates to rank (verbatim texts).",
            },
        },
        "required": ["context", "question", "answers"],
    }

    async def execute(self, args: dict, ctx) -> ToolResult:
        client = _load_client()
        context = str(args.get("context") or "").strip()
        question = str(args.get("question") or "").strip()
        answers = args.get("answers")
        if not context or not question:
            return ToolResult(status="error", result=None,
                              error="context and question are required")
        if (not isinstance(answers, list) or len(answers) < 2
                or not all(isinstance(a, str) for a in answers)):
            return ToolResult(status="error", result=None,
                              error="answers must be a list of 2+ strings")
        try:
            ranked = await asyncio.to_thread(
                client.rank, ctx.config, context, question, answers,
                client.settings(ctx.config)["timeout_s"])
        except client.ClmError as e:
            return ToolResult(status="error", result=None, error=str(e))
        return ToolResult(status="ok", result={"ranked": ranked})
