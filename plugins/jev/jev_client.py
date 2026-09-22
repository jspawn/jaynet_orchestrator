"""Shared decision-model HTTP client for the jev plugin.

Stdlib-only (the plugin declares no pip dependencies): one POST with a state
and typed questions, typed answers back. Same System One contract on both
backends (Open-Jev jev/api.py; OpenRouter's alpha Decisions API speaks the
identical shape with an auth header):

    request : {"model": <id>, "state": <text|obj>,
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

Backends (plugins.jev.backend):
    "" (local)      — your Open-Jev sidecar, POST {base_url}/v1/systemone.
                      Nothing leaves the box.
    "openrouter"    — TypeSafe's hosted Jev via OpenRouter's alpha Decisions
                      API (POST https://openrouter.ai/api/alpha/decisions,
                      model ~typesafe/jev-latest, Bearer OPENROUTER_API_KEY).
                      PRIVACY: the state text leaves the box on every call.
                      The jev.decide TOOL asks like any explicit action, but
                      the routing HOOK refuses this backend unless
                      allow_cloud_route: true — with route:true every
                      incoming request would leave the box before the run's
                      taint/approval machinery exists.

Loaded by file path via _load_client() in hooks.py / tools/jev.py — the
sys.modules cache keeps one module instance (see handoffs/plugins.md).
"""

from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8791"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_MODEL = "~typesafe/jev-latest"


class JevError(Exception):
    """Unreachable server, bad response, or contract violation."""


def settings(config: dict) -> dict:
    """Plugin config section with defaults applied."""
    cfg = ((config or {}).get("plugins") or {}).get("jev") or {}
    backend = str(cfg.get("backend") or "").strip().lower()
    return {
        "backend": backend,
        "base_url": str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        "endpoint": str(cfg.get("endpoint") or OPENROUTER_ENDPOINT),
        "model": str(cfg.get("model") or
                     (OPENROUTER_MODEL if backend == "openrouter"
                      else "open-jev")),
        "api_key_env": str(cfg.get("api_key_env") or "OPENROUTER_API_KEY"),
        "timeout_s": float(cfg.get("timeout_s") or 5),
        # Routing hook: master switch, decision threshold, and its tighter
        # timeout (the hook is on the per-request path). 2s covers cold
        # server calls (~4s on first-ever, ~0.5s warm on a 2B GPU) without
        # letting a wedged server stall run starts. Ships OFF (the recorded
        # 2026-09-22 decision: stay keyword by default).
        "route": bool(cfg.get("route", False)),
        "route_threshold": float(cfg.get("route_threshold") or 0.6),
        "route_timeout_s": float(cfg.get("route_timeout_s") or 2.0),
        # Privacy gate for the routing hook's cloud backend: with
        # backend=openrouter, route:true would send EVERY incoming request's
        # text off-box before the run's taint/approval machinery even
        # exists. Mechanism, not docstring: the hook refuses the cloud
        # backend unless this explicit opt-in is set.
        "allow_cloud_route": bool(cfg.get("allow_cloud_route", False)),
    }


def decide(config: dict, state, questions: dict, timeout_s: float) -> dict:
    """One typed decision round-trip. Raises JevError on any failure —
    callers decide whether that is a tool error or a silent fallback."""
    s = settings(config)
    body = json.dumps({"model": s["model"], "state": state,
                       "questions": questions},
                      allow_nan=False).encode()
    headers = {"Content-Type": "application/json"}
    if s["backend"] == "openrouter":
        key = os.environ.get(s["api_key_env"] or "", "")
        if not key:
            raise JevError(f"openrouter backend needs ${s['api_key_env']} "
                           f"in the environment (jaynet.env)")
        url = s["endpoint"]
        headers["Authorization"] = f"Bearer {key}"
    elif s["backend"]:
        raise JevError(f"unknown jev backend {s['backend']!r} "
                       f"(want '' or 'openrouter')")
    else:
        url = s["base_url"] + "/v1/systemone"
    req = Request(url, data=body, headers=headers)
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            result = json.load(resp)
    except Exception as e:
        where = url if s["backend"] == "openrouter" else (
            f"{s['base_url']} — is `python -m jev.server` running? "
            f"See plugins/jev/README.md.")
        raise JevError(f"jev decision call failed at {where} ({e})") from e
    answers = result.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise JevError("jev response question IDs do not match the request")
    return answers

