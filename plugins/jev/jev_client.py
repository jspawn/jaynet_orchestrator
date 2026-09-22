"""Shared Open-Jev HTTP client for the jev plugin.

Stdlib-only (the plugin declares no pip dependencies): one POST to the
server's /v1/systemone endpoint with a state and typed questions, typed
answers back. Contract (Open-Jev jev/api.py):

    request : {"model": "open-jev", "state": <text|obj>,
               "questions": {qid: {"type": "choice"|"noul"|"score",
                                   "instructions": str,
                                   "criteria": {name: desc} | [levels] |
                                               {"true":…,"false":…}}}}
    response: {"answers": {qid: choice  → {"choice": key,
                                           "probabilities": {key: p},
                                           "confidence": float}
                                 noul   → {"noul": yes_probability}
                                 score  → {"score": expected index,
                                           "probabilities": {...}}}}

Loaded by file path via _load_client() in hooks.py / tools/jev.py — the
sys.modules cache keeps one module instance (see handoffs/plugins.md).
"""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8791"


class JevError(Exception):
    """Unreachable server, bad response, or contract violation."""


def settings(config: dict) -> dict:
    """Plugin config section with defaults applied."""
    cfg = ((config or {}).get("plugins") or {}).get("jev") or {}
    return {
        "base_url": str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        "timeout_s": float(cfg.get("timeout_s") or 5),
        # Routing hook: master switch, decision threshold, and its tighter
        # timeout (the hook is on the per-request path). 2s covers cold
        # server calls (~4s on first-ever, ~0.5s warm on a 2B GPU) without
        # letting a wedged server stall run starts.
        "route": bool(cfg.get("route", True)),
        "route_threshold": float(cfg.get("route_threshold") or 0.6),
        "route_timeout_s": float(cfg.get("route_timeout_s") or 2.0),
    }


def decide(config: dict, state, questions: dict, timeout_s: float) -> dict:
    """One typed decision round-trip. Raises JevError on any failure —
    callers decide whether that is a tool error or a silent fallback."""
    s = settings(config)
    body = json.dumps({"model": "open-jev", "state": state,
                       "questions": questions},
                      allow_nan=False).encode()
    req = Request(s["base_url"] + "/v1/systemone", data=body,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            result = json.load(resp)
    except Exception as e:
        raise JevError(f"Open-Jev server unreachable at {s['base_url']} "
                       f"({e}) — is `python -m jev.server` running? "
                       f"See plugins/jev/README.md.") from e
    answers = result.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise JevError("Open-Jev response question IDs do not match the request")
    return answers
