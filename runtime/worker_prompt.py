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
import re
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


# ---- admin editing (Admin → Prompt → Worker prompts) ----------------------------

NAME_RE = re.compile(r"^(base|[a-z0-9][a-z0-9-]{0,31})$")


def _stem(name: str) -> str:
    return "worker" if name == "base" else f"worker-{name}"


def overlay_path(name: str) -> Path:
    return paths.CUSTOM_DIR / f"{_stem(name)}.md"


def shipped_path(name: str) -> Path:
    return paths.HOME / "prompts" / f"{_stem(name)}.md"


def _pin_path(name: str, config: dict) -> Path | None:
    pin = ((config.get("agent") or {}).get("worker_prompts") or {}).get(name)
    if not pin:
        return None
    p = Path(str(pin))
    return p if p.is_absolute() else (paths.HOME / p)


def parts(config: dict) -> list[dict]:
    """Every editable part: 'base' plus the union of tags that have a shipped
    file, a custom overlay, a config pin, a models.strengths registry entry,
    or a preset carrying the tag. layer is where the EFFECTIVE content comes
    from (pin > custom > shipped > none)."""
    from tools.model.catalog import strength_registry
    names = {"base"}
    for d, pre in ((paths.HOME / "prompts", "shipped"),
                   (paths.CUSTOM_DIR, "custom")):
        try:
            for f in d.glob("worker*.md"):
                names.add("base" if f.stem == "worker"
                          else f.stem[len("worker-"):])
        except OSError:
            pass
    names |= set((config.get("agent") or {}).get("worker_prompts") or {})
    names |= set(strength_registry(config))
    for p in ((config.get("models") or {}).get("presets") or {}).values():
        names |= {str(t) for t in (p.get("strengths") or [])
                  if str(t) != "allround"}
    out = []
    for name in sorted(names):
        pin = _pin_path(name, config)
        layer = ("pin" if pin and pin.is_file()
                 else "custom" if overlay_path(name).is_file()
                 else "shipped" if shipped_path(name).is_file()
                 else "none")
        out.append({"name": name, "layer": layer})
    return out


def describe(name: str, config: dict) -> dict:
    """Editor payload for one part: effective content + provenance."""
    pin = _pin_path(name, config)
    layer, src = "none", None
    if pin and pin.is_file():
        layer, src = "pin", pin
    elif overlay_path(name).is_file():
        layer, src = "custom", overlay_path(name)
    elif shipped_path(name).is_file():
        layer, src = "shipped", shipped_path(name)
    return {"name": name,
            "content": src.read_text(encoding="utf-8", errors="replace")
                       if src else "",
            "layer": layer,
            "overlay_path": str(overlay_path(name)),
            "shipped_path": str(shipped_path(name)),
            "pin_path": str(pin) if pin else None,
            "editable": layer != "pin"}   # a pin lives outside the overlay flow


def save_overlay(name: str, content: str) -> Path:
    """tmp + replace (like gate_prompt.save_overlay): a crash mid-write must
    never leave a truncated prompt for the next delegation to serve."""
    p = overlay_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(p)
    return p


def revert(name: str) -> bool:
    """Delete the overlay (back to the shipped file). False if none."""
    p = overlay_path(name)
    if p.is_file():
        p.unlink()
        return True
    return False
