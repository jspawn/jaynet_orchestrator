"""Worker prompt resolution for specialist sub-agents (agent.worker_prompt).

Default behavior: a specialist.delegate child inherits the FULL orchestrator
gate prompt — routing doctrine included, which a worker must never follow.
With agent.worker_prompt on, the child's base system prompt is the lean
worker prompt instead: base (`prompts/worker.md`, execution discipline only)
plus the tag module for the routed strength (`prompts/worker-<strength>.md`).

Resolution per part ("base" or a strength tag), first hit wins:
1. agent.worker_prompts.<part> — explicit config pin (absolute path, or
   relative to the install root).
2. $JAYNET_DATA/custom/worker[-<part>].md — live overlay (same layering as
   the gate prompt's custom overlay; "base" maps to worker.md).
3. Shipped prompts/worker[-<part>].md in the install root.

Neither part found → None → the child keeps the full gate prompt (pre-flag
behavior), so a missing file never breaks a delegation.
"""

from __future__ import annotations

import logging
from pathlib import Path

from runtime import paths

log = logging.getLogger(__name__)


def _part(name: str, config: dict) -> str | None:
    pin = ((config.get("agent") or {}).get("worker_prompts") or {}).get(name)
    if pin:
        p = Path(str(pin))
        p = p if p.is_absolute() else (paths.HOME / p)
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
        log.warning("agent.worker_prompts.%s pinned to missing file %s", name, p)
    suffix = "" if name == "base" else f"-{name}"
    overlay = paths.CUSTOM_DIR / f"worker{suffix}.md"
    if overlay.is_file():
        return overlay.read_text(encoding="utf-8", errors="replace")
    shipped = paths.HOME / "prompts" / f"worker{suffix}.md"
    if shipped.is_file():
        return shipped.read_text(encoding="utf-8", errors="replace")
    return None


def resolve(strength: str, config: dict) -> str | None:
    """Base prompt + strength tag module, or None when neither exists."""
    base = _part("base", config)
    tag = _part(strength, config) if strength and strength != "base" else None
    if base is None and tag is None:
        return None
    return "\n\n".join(p for p in (base, tag) if p)
