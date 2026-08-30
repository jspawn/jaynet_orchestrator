"""Tool-description overrides — $ORCH_DATA/custom/tool-overrides.yaml.

A small YAML mapping ``<tool.name>: <replacement description>``, applied to
the registry at boot (and immediately on write). This is the apply-target
for accepted eval proposals of class `tool-description`: the judge suggests
better wording for a misleading description, the admin accepts, and the
override lands here — builtin tool code stays pristine and git-managed,
exactly like the gate-prompt overlay and the custom skills/chains layers.

Unknown tool names are ignored (a tool removed upstream leaves a harmless
stale entry; the admin can prune the file by hand).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)


def overrides_path() -> Path:
    from runtime import paths
    return paths.CUSTOM_DIR / "tool-overrides.yaml"


def load() -> dict[str, str]:
    p = overrides_path()
    if not p.is_file():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        log.warning("tool overrides unreadable (%s) — ignoring", e)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def save(overrides: dict[str, str]) -> Path:
    p = overrides_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # tmp + replace (like cloud_store.write_rendered): no truncated live file.
    tmp = p.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(overrides, allow_unicode=True,
                                  sort_keys=True), encoding="utf-8")
    tmp.replace(p)
    return p


def apply(registry, overrides: dict[str, str] | None = None) -> int:
    """Point applicable tool descriptions at their overrides. Returns the
    number applied. Idempotent (call again after registry.reload())."""
    ov = load() if overrides is None else overrides
    applied = 0
    for name, desc in ov.items():
        tool = registry.get(name)
        if tool is not None and desc.strip():
            # Stash the shipped description so a later removal can restore
            # it without re-discovering the registry (which would drop
            # dynamically registered plugin/MCP tools).
            if not hasattr(tool, "_pristine_description"):
                tool._pristine_description = tool.description
            tool.description = desc
            applied += 1
    if applied:
        log.info("applied %d tool-description override(s)", applied)
    return applied


def restore(registry, name: str) -> bool:
    """Restore a tool's shipped description after its override was removed.
    True when a live tool was restored; False for stale entries (unknown
    tool — nothing was ever applied)."""
    tool = registry.get(name)
    pristine = getattr(tool, "_pristine_description", None) if tool else None
    if pristine is None:
        return False
    tool.description = pristine
    del tool._pristine_description
    return True
