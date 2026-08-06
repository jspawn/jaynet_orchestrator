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

import yaml

log = logging.getLogger(__name__)


def overrides_path() -> "Path":
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


def save(overrides: dict[str, str]) -> "Path":
    p = overrides_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(overrides, allow_unicode=True,
                                sort_keys=True), encoding="utf-8")
    return p


def apply(registry, overrides: dict[str, str] | None = None) -> int:
    """Point applicable tool descriptions at their overrides. Returns the
    number applied. Idempotent (call again after registry.reload())."""
    ov = load() if overrides is None else overrides
    applied = 0
    for name, desc in ov.items():
        tool = registry.get(name)
        if tool is not None and desc.strip():
            tool.description = desc
            applied += 1
    if applied:
        log.info("applied %d tool-description override(s)", applied)
    return applied
