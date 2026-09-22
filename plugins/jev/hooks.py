"""Jev plugin hooks — decision-model request routing.

route_request asks the Open-Jev server which strength a request needs
(choice over the models.strengths registry + a 'general' escape candidate)
and returns the tag when the calibrated probability clears the threshold.
None — on a 'general' pick, a low confidence, or any server problem — falls
through to the keyword router. Fired via asyncio.to_thread from the loop,
so this blocking HTTP call is allowed; it is still bounded hard
(route_timeout_s, default 0.8s) because it runs once per run start.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_client():
    """Shared jev_client module — single sys.modules entry (see
    handoffs/plugins.md: fresh execs split module state)."""
    name = "jev_plugin_client"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parent / "jev_client.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


GENERAL = "general"


def route_request(user_message, config):
    """route_request hook: strength tag for the request, or None."""
    client = _load_client()
    s = client.settings(config if isinstance(config, dict) else {})
    if not s["route"]:
        return None
    msg = (user_message or "").strip()
    if not msg:
        return None
    strengths = ((config.get("models") or {}).get("strengths") or {})
    criteria = {str(tag): str(desc) for tag, desc in strengths.items()
                if str(tag) != "allround"}
    if not criteria:
        return None
    criteria[GENERAL] = ("no specialist needed — plain chat, questions, or a "
                         "simple task the orchestrator handles itself")
    try:
        answers = client.decide(
            config, msg[:4000],
            {"route": {
                "type": "choice",
                "instructions": "Which specialist strength does this request "
                                "most need? Choose 'general' when no "
                                "specialist is needed.",
                "criteria": criteria}},
            s["route_timeout_s"])
    except client.JevError:
        return None
    answer = answers.get("route") or {}
    choice = str(answer.get("choice") or "")
    probs = answer.get("probabilities") or {}
    if not choice or choice == GENERAL:
        return None
    try:
        prob = float(probs.get(choice) or 0)
    except (TypeError, ValueError):
        return None
    return choice if prob >= s["route_threshold"] else None
