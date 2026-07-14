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

    def select(self, user_message: str, requested: list[str] | None = None,
               disabled: set[str] | None = None) -> list[str] | None:
        """Return a frozen allowlist of tool names, or None meaning 'all tools'.

        Called once, before the loop starts. The result is held constant for the
        whole run so the tool-schema prefix never changes mid-conversation.

        disabled: globally disabled tools to exclude even in auto mode.
        """
        names = [t.name for t in self.registry.all()]
        if disabled:
            names = [n for n in names if n not in disabled]

        if requested:                       # explicit allowlist always wins
            allow = self._expand(requested, names)
            chosen_via = "static"
            self._diag = {"via": "static", "requested": requested}
        elif self.mode == "auto":
            allow, diag = self._auto(user_message, names)
            chosen_via = "auto"
            self._diag = diag
        else:                               # "all" or "static" without a list
            self._diag = {"via": self.mode, "note": "no filtering"}
            return None

        # Preserve registry order, drop anything unknown, apply optional cap.
        ordered = [n for n in names if n in allow]
        if self.max_tools:
            ordered = ordered[: self.max_tools]

        log.info("Tool selection (%s): %d/%d tools -> %s",
                 chosen_via, len(ordered), len(names), ordered)
        # Empty selection would leave the model with no tools at all; fall back
        # to 'all' rather than strand it.
        if not ordered:
            self._diag["fallback"] = "empty→all"
            return None
        return ordered

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

    def _auto(self, user_message: str, names: list[str]) -> tuple[set[str], dict]:
        """Deterministic heuristic: core namespaces + keyword-triggered ones.

        core_namespaces can contain both namespace prefixes (e.g. 'web') that
        expand to all web.* tools, and exact tool names (e.g. 'web.search').

        Returns (allowed_set, diagnostics_dict).
        """
        msg = (user_message or "").lower()
        allow: set[str] = set()
        core_matched: list[str] = []
        core_missed: list[str] = []
        for c in self.core:
            if "." in c:                            # exact tool name
                if c in names:
                    allow.add(c)
                    core_matched.append(c)
                else:
                    core_missed.append(c)
            else:                                   # namespace prefix
                expanded = [n for n in names if n.split(".", 1)[0] == c]
                allow.update(expanded)
                if expanded:
                    core_matched.append(f"{c}→{len(expanded)}")
                else:
                    core_missed.append(c)
        kw_triggered: dict[str, str] = {}
        for ns, kws in self.keywords.items():
            for kw in kws:
                if kw.lower() in msg:
                    count_before = len(allow)
                    allow.update(n for n in names if n.split(".", 1)[0] == ns)
                    count_after = len(allow)
                    kw_triggered[ns] = kw
                    break  # one keyword is enough per namespace
        diag = {
            "via": "auto",
            "core_count": len([c for c in core_matched if "→" not in c]) + sum(int(c.split("→")[1]) for c in core_matched if "→" in c),
            "core_matched": core_matched,
            "core_missed": core_missed,
            "kw_triggered": kw_triggered,
            "total": len(allow),
        }
        return allow, diag
