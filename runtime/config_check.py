"""Boot-time config preparation for config/runtime.yaml.

AgentRuntime calls warn_unknown_sections right after the YAML load; it is
the single load-time hook every consumer shares, so it does two things:

1. Anchors relative config paths via config_loader.resolve_paths
   (ORCH_HOME/ORCH_DATA — see that module). Absolute paths pass through.
2. Typo guard: a misspelled top-level key is silently ignored by every
   consumer (they all use config.get("section") with defaults), so warn
   loudly at boot instead. Warning-only: unknown sections are kept — they
   may be forward-compatible keys from a newer version.
3. Typed-section validation (runtime/config_schema.py, audit item 8): the
   agent/budgets/tool_selection/eval sections are coerced in place against
   their pydantic models and nested-key typos warn with a did-you-mean
   hint. Runs HERE (after the admin config overrides merge in AgentRuntime)
   so override values are covered too, not just the raw YAML.
"""
from __future__ import annotations

import difflib

from runtime.config_loader import resolve_paths
from runtime.config_schema import validate_typed_sections

# Top-level sections of the shipped config/runtime.yaml.
KNOWN_SECTIONS = frozenset({
    "orchestrator", "budgets", "loop_guard", "goal", "watchdog", "agent",
    "skills", "chains", "models", "architect", "compaction", "parallel_tools",
    "tool_selection", "privacy", "confirmation", "security", "voice",
    "processes", "trace", "web", "tools", "costs", "verify", "council",
    "eval", "plugins", "reflect",
})


def warn_unknown_sections(config: dict, log) -> list[str]:
    """Anchor relative paths (resolve_paths), validate/coerce the typed
    sections in place (unknown/typo'd nested keys + uncoercible values warn,
    nothing is rejected — audit item 8), then log a warning per unknown
    top-level key and return them. Called once per AgentRuntime boot."""
    resolve_paths(config)
    if not isinstance(config, dict):
        return []
    validate_typed_sections(config, log)
    unknown = [k for k in config if k not in KNOWN_SECTIONS]
    for key in unknown:
        hint = ""
        close = difflib.get_close_matches(str(key), KNOWN_SECTIONS, n=1)
        if close:
            hint = f" — did you mean '{close[0]}'?"
        log.warning("unknown config section '%s'%s", key, hint)
    return unknown
