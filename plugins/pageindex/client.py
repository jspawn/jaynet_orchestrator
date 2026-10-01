"""pageindex SDK client — config resolution + lazy client factory.

The `pageindex` pip package (MIT) builds a hierarchical TREE INDEX of a long
document with an LLM; the JayNet brain then navigates that tree via the
doc.* tools (titles/summaries/page ranges → exact pages) instead of
vector-similarity chunk retrieval. Indexing goes through the JayNet LiteLLM
proxy — the SDK's local mode just needs an OpenAI-compatible endpoint.

The SDK is imported INSIDE get_client() so loading the plugin never requires
the package: the manifest's dependency check reports it as missing instead.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_DEFAULTS = {
    "model": "openai/local-specialist",  # litellm model string; the alias after
                                         # openai/ is served by the JayNet proxy
    "storage_path": "",                  # "" = <data>/pageindex
    "api_base": "",                      # "" = orchestrator.litellm_base
    "api_key": "",                       # "" = $LITELLM_MASTER_KEY or sk-local
}


def settings(config: dict) -> dict:
    cfg = ((config or {}).get("plugins") or {}).get("pageindex") or {}
    out: dict[str, Any] = dict(_DEFAULTS)
    for k in out:
        if k in cfg and cfg[k] is not None:
            out[k] = cfg[k]
    if not str(out["storage_path"] or "").strip():
        from runtime.paths import DATA
        out["storage_path"] = str(DATA / "pageindex")
    if not str(out["api_base"] or "").strip():
        from runtime.paths import LITELLM_BASE
        out["api_base"] = str(((config or {}).get("orchestrator") or {})
                              .get("litellm_base") or LITELLM_BASE).rstrip("/")
    if not str(out["api_key"] or "").strip():
        # Local servers accept any non-empty key; the proxy may be keyless on
        # localhost (same rule as graphify/model_client).
        out["api_key"] = os.environ.get("LITELLM_MASTER_KEY") or "sk-local"
    return out


class PageIndexError(Exception):
    """Missing SDK, or the client can't be constructed."""


# One client per resolved settings tuple — the SDK holds a storage handle and
# the connection to the proxy, so rebuilding it per call would be wasteful.
_CLIENTS: dict[tuple, Any] = {}


def get_client(config: dict):
    try:
        from pageindex import PageIndexClient
    except ImportError as e:
        raise PageIndexError(
            "the 'pageindex' pip package is not installed — run "
            "`pip install pageindex` into the JayNet venv and restart "
            "(see plugins/pageindex/README.md)") from e
    s = settings(config)
    key = (s["model"], s["storage_path"], s["api_base"], s["api_key"])
    client = _CLIENTS.get(key)
    if client is None:
        Path(s["storage_path"]).mkdir(parents=True, exist_ok=True)
        client = PageIndexClient(index={
            "model": s["model"],
            "storage_path": s["storage_path"],
            "backend": {"api_base": s["api_base"], "api_key": s["api_key"]},
        })
        _CLIENTS[key] = client
    return client
