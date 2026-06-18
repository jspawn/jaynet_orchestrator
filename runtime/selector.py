"""Per-run tool selection.

Decide ONCE per request which tools to expose to the orchestrator, then freeze
that set for the whole run. Freezing is the point: the tool schemas are a stable
prefix, so a fixed set stays prompt-cache-friendly. Changing the toolset
mid-run would bust the prefix cache and usually costs more than it saves.

Modes (config: tool_selection.mode):
  all     - expose everything (default; zero behaviour change)
  static  - expose only the caller-provided allowlist (--tools); else all
  auto    - deterministic keyword->namespace heuristic on the user message,
            plus a configured always-on core set. No extra LLM call, so the
            decision is cheap, reproducible, and cache-stable within a run.

An explicit caller allowlist (e.g. `--tools web,code`) always wins, regardless
of mode. Namespace shorthands expand: `web` -> every `web.*` tool.
"""

from __future__ import annotations

import logging

from .registry import ToolRegistry

log = logging.getLogger(__name__)


class ToolSelector:
    def __init__(self, registry: ToolRegistry, config: dict):
        self.registry = registry
        sel = config.get("tool_selection") or {}
        self.mode: str = sel.get("mode", "all")
        # Namespaces always exposed in `auto` mode. `llm` by default because
        # delegating to cloud models is the orchestrator's primary job —
        # hiding it would cripple the model more than it would save.
        self.core: set[str] = set(sel.get("core_namespaces", ["llm"]))
        # {namespace: [keyword, ...]} — if any keyword appears in the user
        # message, that namespace's tools are added (auto mode).
        self.keywords: dict[str, list[str]] = sel.get("keyword_namespaces", {})
        # Optional hard cap on number of tools exposed (None = unlimited).
        self.max_tools: int | None = sel.get("max_tools")

    def select(self, user_message: str, requested: list[str] | None = None) -> list[str] | None:
        """Return a frozen allowlist of tool names, or None meaning 'all tools'.

        Called once, before the loop starts. The result is held constant for the
        whole run so the tool-schema prefix never changes mid-conversation.
        """
        names = [t.name for t in self.registry.all()]

        if requested:                       # explicit allowlist always wins
            allow = self._expand(requested, names)
            chosen_via = "static"
        elif self.mode == "auto":
            allow = self._auto(user_message, names)
            chosen_via = "auto"
        else:                               # "all" or "static" without a list
            return None

        # Preserve registry order, drop anything unknown, apply optional cap.
        ordered = [n for n in names if n in allow]
        if self.max_tools:
            ordered = ordered[: self.max_tools]

        log.info("Tool selection (%s): %d/%d tools -> %s",
                 chosen_via, len(ordered), len(names), ordered)
        # Empty selection would leave the model with no tools at all; fall back
        # to 'all' rather than strand it.
        return ordered or None

    # ---------- internals ----------

    def _expand(self, requested: list[str], names: list[str]) -> set[str]:
        """Expand a caller list of exact names and/or namespace shorthands."""
        allow: set[str] = set()
        valid_ns = {n.split(".", 1)[0] for n in names}
        for raw in requested:
            item = raw.strip()
            if not item:
                continue
            if item in names:                       # exact tool name
                allow.add(item)
            elif item in valid_ns:                  # namespace shorthand
                allow.update(n for n in names if n.split(".", 1)[0] == item)
            else:
                log.warning("Requested tool/namespace not found: %s", item)
        return allow

    def _auto(self, user_message: str, names: list[str]) -> set[str]:
        """Deterministic heuristic: core namespaces + keyword-triggered ones."""
        msg = (user_message or "").lower()
        allow = {n for n in names if n.split(".", 1)[0] in self.core}
        for ns, kws in self.keywords.items():
            if any(kw.lower() in msg for kw in kws):
                allow.update(n for n in names if n.split(".", 1)[0] == ns)
        return allow
