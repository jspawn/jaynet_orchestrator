"""Shared CLM HTTP client for the clm plugin.

Stdlib-only (the plugin declares no pip dependencies): one POST with a state
and typed questions, typed answers back — the System One contract clm-serve
exposes on POST {base_url}/v1/systemone (wire-compatible with TypeSafe/Jev,
which is why this file looks like jev_client's sibling):

    request : {"model": "clm-latest", "state": <text|obj>,
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

Plus POST {base_url}/v1/rank for free-form candidates:
    request : {"context": …, "question": …, "answers": [candidate, …]}
    response: {"ranked": [{"rank", "candidate", "prob"}, …]}  (best first)

Backend: your local clm-serve pair (clm-serve on :8700 + a Qwen3-8B pooling
encoder — see plugins/clm/README.md). Nothing leaves the box; there is no
cloud backend for this plugin.

Loaded by file path via _load_client() in hooks.py / tools/clm.py — the
sys.modules cache keeps one module instance (see handoffs/plugins.md).
"""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8700"
DEFAULT_MODEL = "clm-latest"


class ClmError(Exception):
    """Unreachable server, bad response, or contract violation."""


def settings(config: dict) -> dict:
    """Plugin config section with defaults applied."""
    cfg = ((config or {}).get("plugins") or {}).get("clm") or {}
    return {
        "base_url": str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        "model": str(cfg.get("model") or DEFAULT_MODEL),
        "timeout_s": float(cfg.get("timeout_s") or 5),
        # Routing hook: master switch, decision threshold, and its tighter
        # timeout (the hook is on the per-request path). clm-serve answers a
        # warm choice call in ~30ms (p50, one RTX 4090 — the encoder on CPU
        # is slower but still far under the bound). Ships OFF: keyword
        # routing stays the default until the A/B proves itself.
        "route": bool(cfg.get("route", False)),
        "route_threshold": float(cfg.get("route_threshold") or 0.6),
        "route_timeout_s": float(cfg.get("route_timeout_s") or 2.0),
    }


def _post(config: dict, path: str, body: dict, timeout_s: float) -> dict:
    s = settings(config)
    req = Request(s["base_url"] + path,
                  data=json.dumps(body, allow_nan=False).encode(),
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            return json.load(resp)
    except Exception as e:
        raise ClmError(
            f"clm call failed at {s['base_url']} — is `clm-serve` running "
            f"(and its Qwen3-8B encoder up)? See plugins/clm/README.md. "
            f"({e})") from e


def decide(config: dict, state, questions: dict, timeout_s: float) -> dict:
    """One typed decision round-trip. Raises ClmError on any failure —
    callers decide whether that is a tool error or a silent fallback."""
    s = settings(config)
    result = _post(config, "/v1/systemone",
                   {"model": s["model"], "state": state,
                    "questions": questions}, timeout_s)
    answers = result.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ClmError("clm response question IDs do not match the request")
    return answers


def rank(config: dict, context, question: str, answers: list,
         timeout_s: float) -> list:
    """Rank free-form candidates against a state, best first. Raises
    ClmError on any failure."""
    result = _post(config, "/v1/rank",
                   {"context": context, "question": question,
                    "answers": answers}, timeout_s)
    ranked = result.get("ranked")
    if not isinstance(ranked, list):
        raise ClmError("clm rank response misses the 'ranked' list")
    return ranked
