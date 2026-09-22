"""jev.decide — typed probability decisions via a local Open-Jev server.

The brain can consult the decision model directly: choice (pick from
candidates with probabilities), noul (calibrated yes/no), score (ordered
levels). One forward pass per question, no text generation, nothing leaves
the box (the server is local; see plugins/jev/README.md for setup).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from runtime.tool_base import Tool, ToolResult


def _load_client():
    """Shared jev_client module — single sys.modules entry (see
    handoffs/plugins.md: fresh execs split module state)."""
    name = "jev_plugin_client"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parent.parent / "jev_client.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


class JevDecide(Tool):
    name = "jev.decide"
    description = (
        "Ask the local Open-Jev decision model: give it a `state` (the text "
        "to judge) and typed `questions` — it answers with calibrated "
        "PROBABILITIES in one forward pass, no generated text. Question "
        "types: choice (criteria = {candidate: description} → probability "
        "per candidate + top choice), noul (yes/no probability), score "
        "(criteria = ordered level descriptions → expected level). Use it "
        "for routing, triage, or any classification you want a confidence "
        "number on — much cheaper and more honest than asking an LLM to "
        "guess JSON. Requires the Open-Jev sidecar server "
        "(plugins/jev/README.md); returns a clear error when it is down.")
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
        except client.JevError as e:
            return ToolResult(status="error", result=None, error=str(e))
        return ToolResult(status="ok", result={"answers": answers})
