"""Tool registry with plugin auto-discovery.

Scans /srv/orchestrator/tools/<namespace>/*.py at startup, imports each module,
and registers any concrete Tool subclasses found.

To add a new tool:
1. Create /srv/orchestrator/tools/<namespace>/<verb>.py
2. Subclass Tool, set name = "<namespace>.<verb>", implement execute()
3. Restart the runtime (or call registry.reload())
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
from pathlib import Path

from .tool_base import Tool

log = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self, tools_root: str | Path):
        self.tools_root = Path(tools_root)
        self._tools: dict[str, Tool] = {}

    def discover(self) -> None:
        """Walk the tools directory and import every .py file (except __init__)."""
        self._tools.clear()
        if not self.tools_root.exists():
            log.warning("Tools root does not exist: %s", self.tools_root)
            return

        # Ensure parent of tools/ is importable so "tools.<ns>.<mod>" resolves.
        parent = str(self.tools_root.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)

        for py_file in sorted(self.tools_root.rglob("*.py")):
            if py_file.name.startswith("_"):
                continue
            # Build module path relative to tools_root.parent
            rel = py_file.relative_to(self.tools_root.parent).with_suffix("")
            mod_name = ".".join(rel.parts)
            try:
                module = importlib.import_module(mod_name)
            except Exception as e:
                log.error("Failed to import %s: %s", mod_name, e)
                continue

            for _, obj in inspect.getmembers(module, inspect.isclass):
                # Concrete Tool subclasses defined in this module (not imports)
                if (issubclass(obj, Tool) and obj is not Tool
                        and obj.__module__ == module.__name__
                        and not inspect.isabstract(obj)):
                    instance = obj()
                    if not instance.name:
                        log.warning("Tool class %s has empty name, skipping", obj)
                        continue
                    if instance.name in self._tools:
                        log.warning("Duplicate tool name %s (replacing)", instance.name)
                    self._tools[instance.name] = instance
                    log.info("Registered tool: %s", instance.name)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def openai_schemas(self, allowed: list[str] | None = None) -> list[dict]:
        """Render tools as OpenAI tool definitions."""
        tools = self._tools.values() if allowed is None else \
                [t for n, t in self._tools.items() if n in allowed]
        return [t.to_openai_schema() for t in tools]
