"""Hook registry — the ONLY seam between core and plugins.

Plugins (runtime/plugins.py) register plain callables for named hooks; core
fires them at well-defined points. A plugin never touches core internals, and
core never imports plugin code directly — this module is the boundary.

Hook names v1 (signatures documented at the fire sites):

    augment_project_context(owner, pid, meta, files_root) -> str | None
        Fired while building the [Project: …] prompt prefix (web/server.py
        _augment_with_project). Non-empty returns are appended to the prefix.
    on_project_delete(owner, pid)
        Fired after a project dir was deleted (web/routes_projects.py).
    on_project_file_changed(owner, pid, path)
        Fired after a project file write/delete/rename via the web API.

Every fire wraps each callable in try/except: a throwing plugin is logged and
skipped, never breaks a run. Hooks fire synchronously on the caller's thread —
keep them fast (mark state, don't do work).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

HOOK_NAMES = (
    "augment_project_context",
    "on_project_delete",
    "on_project_file_changed",
)

_REGISTRY: dict[str, list[Callable]] = {name: [] for name in HOOK_NAMES}


def register(name: str, fn: Callable) -> bool:
    """Attach `fn` to hook `name`. Unknown hook names are refused (typo guard)."""
    if name not in _REGISTRY:
        log.warning("Refusing to register unknown hook %r", name)
        return False
    _REGISTRY[name].append(fn)
    return True


def fire(name: str, *args: Any, **kwargs: Any) -> list[Any]:
    """Call every registered callable for `name`, returning their non-None
    results in registration order. Exceptions are logged and skipped."""
    out: list[Any] = []
    for fn in _REGISTRY.get(name, ()):
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            log.error("Hook %s callable %r failed: %s", name, fn, e)
            continue
        if result is not None:
            out.append(result)
    return out


def registered(name: str) -> list[Callable]:
    """The callables currently attached to `name` (read-only inspection)."""
    return list(_REGISTRY.get(name, ()))


def clear() -> None:
    """Detach everything (tests; plugin reload)."""
    for fns in _REGISTRY.values():
        fns.clear()
