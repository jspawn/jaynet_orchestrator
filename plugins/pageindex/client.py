"""pageindex SDK client — config resolution + lazy client factory.

The `pageindex` pip package (MIT) builds a hierarchical TREE INDEX of a long
document with an LLM; the JayNet brain then navigates that tree via the
doc.* tools (titles/summaries/page ranges → exact pages) instead of
vector-similarity chunk retrieval. Indexing goes through the JayNet LiteLLM
proxy — the SDK's local mode just needs an OpenAI-compatible endpoint.

The SDK is imported INSIDE get_client() so loading the plugin never requires
the package: the manifest's dependency check reports it as missing instead.

Owner scoping (audit 2026-10-05 finding 3): with no storage_path configured,
each web account's indexes live in <data>/pageindex/<owner> — one account's
doc.tree/doc.pages can never resolve another's doc_id. The ownerless
CLI/token path keeps the legacy <data>/pageindex root; indexes built before
the scoping change stay there (reachable from the CLI path; web accounts
re-index on demand — an index is one rebuild from the source PDF).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_DEFAULTS = {
    "model": "openai/local-specialist",  # litellm model string; the alias after
                                         # openai/ is served by the JayNet proxy
    "storage_path": "",                  # "" = <data>/pageindex[/<owner>]
    "api_base": "",                      # "" = orchestrator.litellm_base
    "api_key": "",                       # "" = $LITELLM_MASTER_KEY or sk-local
}


def _owner_dir(owner: str) -> str:
    """Owner as a safe single path segment (usernames are free-form)."""
    d = re.sub(r"[^A-Za-z0-9_.-]", "_", str(owner or "")).lstrip(".")
    return d or "_"


def settings(config: dict, owner: str = "") -> dict:
    cfg = ((config or {}).get("plugins") or {}).get("pageindex") or {}
    out: dict[str, Any] = dict(_DEFAULTS)
    for k in out:
        if k in cfg and cfg[k] is not None:
            out[k] = cfg[k]
    if not str(out["storage_path"] or "").strip():
        from runtime.paths import DATA
        base = DATA / "pageindex"
        out["storage_path"] = str(base / _owner_dir(owner) if owner else base)
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


def model_alias(config: dict) -> str:
    """The LiteLLM alias indexing traffic goes to. The model string is
    "openai/<alias>" (provider prefix + the alias the JayNet proxy serves) —
    strip the prefix for locality checks."""
    m = str(settings(config)["model"] or "").strip()
    return m.split("/", 1)[-1] if "/" in m else m


def privacy_refusal(config: dict, ctx) -> str | None:
    """Cloud gate on the indexing LLM call path (audit #27 C3): doc.index
    build sends the WHOLE document text to whatever alias
    plugins.pageindex.model names — "nothing leaves the box" only holds while
    that alias is local. A cloud alias follows the core rules
    (runtime/cloud_gate): a private-tainted run without share_private is
    REFUSED here (a tool cannot offer the per-call privacy approval the
    loop's llm.call gate can), and the standard confirm_cloud_calls approval
    is requested earlier via the tool's needs_confirmation. Local aliases
    never gate."""
    from runtime import cloud_gate
    alias = model_alias(config)
    if cloud_gate.is_local_alias(alias, config):
        return None
    return cloud_gate.privacy_refusal(ctx, [alias])


# One client per resolved settings tuple — the SDK holds a storage handle and
# the connection to the proxy, so rebuilding it per call would be wasteful.
_CLIENTS: dict[tuple, Any] = {}


def get_client(config: dict, owner: str = ""):
    try:
        from pageindex import PageIndexClient
    except ImportError as e:
        raise PageIndexError(
            "the 'pageindex' pip package is not installed — run "
            "`pip install pageindex` into the JayNet venv and restart "
            "(see plugins/pageindex/README.md)") from e
    except Exception as e:
        # Present but broken (a dependency of the SDK raising at import time
        # surfaces as something other than ImportError) — report it as a
        # broken install, not as "not installed".
        raise PageIndexError(
            f"the 'pageindex' package failed to import "
            f"({type(e).__name__}: {e}) — the install looks broken; "
            "reinstall it into the JayNet venv and restart "
            "(see plugins/pageindex/README.md)") from e
    s = settings(config, owner)
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
