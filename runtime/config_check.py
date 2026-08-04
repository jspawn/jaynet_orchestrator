"""Boot-time typo guard for config/runtime.yaml.

A misspelled top-level key is silently ignored by every consumer (they all
use config.get("section") with defaults), so warn loudly at boot instead.
Warning-only: unknown sections are kept — they may be forward-compatible
keys from a newer version.
"""
from __future__ import annotations

import difflib

# Top-level sections of the shipped config/runtime.yaml.
KNOWN_SECTIONS = frozenset({
    "orchestrator", "budgets", "loop_guard", "goal", "watchdog", "agent",
    "skills", "chains", "models", "architect", "compaction", "parallel_tools",
    "tool_selection", "privacy", "confirmation", "voice", "processes",
    "trace", "web", "tools", "costs", "verify", "council",
})


def warn_unknown_sections(config: dict, log) -> list[str]:
    """Log a warning per unknown top-level key; return them."""
    if not isinstance(config, dict):
        return []
    unknown = [k for k in config if k not in KNOWN_SECTIONS]
    for key in unknown:
        hint = ""
        close = difflib.get_close_matches(str(key), KNOWN_SECTIONS, n=1)
        if close:
            hint = f" — did you mean '{close[0]}'?"
        log.warning("unknown config section '%s'%s", key, hint)
    return unknown
