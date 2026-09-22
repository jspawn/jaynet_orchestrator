"""Jev plugin hooks — decision-model request routing.

route_request asks the Open-Jev server which strength a request needs
(choice over the models.strengths registry + a 'general' escape candidate)
and returns the tag when the calibrated probability clears the threshold.
None — on a 'general' pick, a low confidence, or any server problem — falls
through to the keyword router. Fired via asyncio.to_thread from the loop,
so this blocking HTTP call is allowed; it is still bounded hard
(route_timeout_s, default 2.0s) because it runs once per run start.

Privacy: the hook fires at run START, before the run's taint/approval
machinery exists. With backend=openrouter the request text would leave the
box on every run — so the hook refuses the cloud backend unless
plugins.jev.allow_cloud_route: true is set explicitly (the jev.decide tool
is an explicit per-call action and is not gated this way).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)


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
    if s["backend"] == "openrouter" and not s["allow_cloud_route"]:
        # Cloud routing sends EVERY request's text off-box at run start,
        # before taint/approval can exist — explicit opt-in only, otherwise
        # fall through to the keyword router (logged once per process).
        if not getattr(route_request, "_cloud_warned", False):
            route_request._cloud_warned = True
            log.warning("jev route_request: openrouter backend refused "
                        "(plugins.jev.allow_cloud_route is not true) — "
                        "keyword routing continues")
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
