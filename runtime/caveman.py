"""Scoped Caveman Compression instructions for model-internal communication.

This implements the upstream specification as a generation-time controlled
language, not as post-hoc stopword stripping. Removing words after the fact can
silently drop a negation or an inference step; asking the model for the compact
form preserves the intended logic while avoiding a second compression call.

User-facing answers and exact artifacts stay outside the compression scope.
Spec: https://github.com/wilpel/caveman-compression/blob/main/SPEC.md
"""

from __future__ import annotations

_INSTRUCTIONS = {
    "reasoning": (
        "## Caveman reasoning\n"
        "Hidden reasoning only: use Caveman Compression. Atomic 2–5-word "
        "sentences. Remove filler, never facts. Keep every inference step "
        "explicit. Preserve numbers, names, paths, constraints, negations, "
        "uncertainty, code and tool syntax. Never use this style in "
        "user-visible commentary or final answers."
    ),
    "handoff_out": (
        "## Caveman delegation\n"
        "When writing a task for another model, use Caveman Compression for "
        "free-form prose. Keep the handoff complete: context, paths, "
        "constraints and done-checks stay explicit. Never compress code, "
        "JSON, commands or verbatim evidence."
    ),
    "handoff_in": (
        "## Caveman handoff\n"
        "Your final report goes to another model, not directly to the user. "
        "Use Caveman Compression for free-form prose: atomic facts, no "
        "filler, every requirement, risk and uncertainty preserved. Keep "
        "paths, URLs, commands, JSON, CHECK lines and verbatim evidence "
        "exact. Do not alter code or structured data."
    ),
    "model_to_model": (
        "## Caveman model conversation\n"
        "For free-form natural-language replies to another model, use Caveman "
        "Compression: short atomic sentences, no filler, all facts and "
        "inference steps preserved. Keep the requested output format "
        "unchanged: code, JSON, schemas, commands and verbatim quotations "
        "stay exact."
    ),
}
_SCOPE_FLAGS = {
    "reasoning": "reasoning",
    "handoff_out": "handoffs",
    "handoff_in": "handoffs",
    "model_to_model": "model_to_model",
}


def enabled(config: dict | None, scope: str) -> bool:
    """One scoped switch, gated by the master switch.

    A missing section means OFF for backward compatibility with old/minimal
    configs. The shipped runtime.yaml carries the section with all scopes on.
    """
    if scope not in _SCOPE_FLAGS or not isinstance(config, dict):
        return False
    section = config.get("caveman")
    if not isinstance(section, dict):
        return False
    return bool(section.get("enabled", True)
                and section.get(_SCOPE_FLAGS[scope], True))


def instruction(config: dict | None, scope: str) -> str:
    """The enabled scope's prompt block, including leading separation."""
    return ("\n\n" + _INSTRUCTIONS[scope]) if enabled(config, scope) else ""


def append(system: str, config: dict | None, scope: str) -> str:
    """Append a scope block without changing the caller's base prompt."""
    return system + instruction(config, scope)
