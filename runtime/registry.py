"""Tool registry with plugin auto-discovery.

Scans /srv/orchestrator/tools/<namespace>/*.py at startup, imports each module,
and registers any concrete Tool subclasses found.

To add a new tool:
1. Create /srv/orchestrator/tools/<namespace>/<verb>.py
2. Subclass Tool, set name = "<namespace>.<verb>", implement execute()
3. Restart the runtime (or call registry.reload())

A second, custom layer lives outside the repo (ORCH_DATA/custom, see
runtime.paths): discover_extra() loads admin-written Python tools by file
path (the data dir is not a package), and register_instance() takes
already-built Tool objects (declarative API connectors). Both refuse to
overwrite an already-registered name.
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

    def discover_extra(self, extra_dir: str | Path) -> None:
        """Load custom Python tools from a directory that is NOT a package
        (e.g. ORCH_DATA/custom/tools). Each *.py is imported from its file
        path; concrete Tool subclasses register as in discover(), except a
        name that is already taken is refused (log + skip) instead of
        replaced. Broken files are logged and skipped, never fatal."""
        root = Path(extra_dir)
        if not root.is_dir():
            return
        for py_file in sorted(root.rglob("*.py")):
            if py_file.name.startswith("_"):
                continue
            rel = py_file.relative_to(root).with_suffix("")
            mod_name = "orch_custom_" + "_".join(rel.parts)
            try:
                spec = importlib.util.spec_from_file_location(mod_name, py_file)
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
            except Exception as e:
                log.error("Failed to load custom tool %s: %s", py_file, e)
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (issubclass(obj, Tool) and obj is not Tool
                        and obj.__module__ == module.__name__
                        and not inspect.isabstract(obj)):
                    try:
                        instance = obj()
                    except Exception as e:
                        log.error("Failed to instantiate custom tool %s: %s", obj, e)
                        continue
                    if not instance.name:
                        log.warning("Custom tool class %s has empty name, skipping", obj)
                        continue
                    if instance.name in self._tools:
                        log.warning("Custom tool %s from %s collides with an "
                                    "existing tool — skipped",
                                    instance.name, py_file)
                        continue
                    self._tools[instance.name] = instance
                    log.info("Registered custom tool: %s", instance.name)

    def register_instance(self, tool: Tool) -> bool:
        """Register an already-instantiated tool (e.g. a connector built from
        YAML). Refuses to overwrite an existing name (log + skip)."""
        if not tool.name:
            log.warning("Tool instance %r has empty name, skipping", tool)
            return False
        if tool.name in self._tools:
            log.warning("Refusing to overwrite existing tool %s", tool.name)
            return False
        self._tools[tool.name] = tool
        log.info("Registered tool: %s", tool.name)
        return True

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def openai_schemas(self, allowed: list[str] | None = None) -> list[dict]:
        """Render tools as OpenAI tool definitions."""
        tools = self._tools.values() if allowed is None else \
                [t for n, t in self._tools.items() if n in allowed]
        return [t.to_openai_schema() for t in tools]
