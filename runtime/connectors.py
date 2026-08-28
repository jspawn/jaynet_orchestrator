"""Connector state + registry wiring — the box-level half of connector
packages (tools/connector/__init__.py owns the shareable half).

State lives in $ORCH_DATA/custom/connectors.json:

    {"<id>": {"enabled": true, "mode": "ro"|"rw", "settings": {"KEY": "val"}}}

Box state is deliberately separate from the package files: packs stay
shareable (a .jayconn never carries your base URLs or env names), while
each box toggles, permits and configures its own. `refresh()` applies the
state to the tool registry — called at boot and after every admin change
(hot, no restart; in-flight runs keep their frozen tool set, new runs see
the change — same semantics as plugin hot-toggle).
"""

from __future__ import annotations

import json
import logging

from runtime import paths
from tools.connector import load_packages

log = logging.getLogger(__name__)

_registered: set[str] = set()        # connector-owned tool names currently live


def _state_path():
    return paths.CUSTOM_DIR / "connectors.json"


def load_state() -> dict:
    p = _state_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("connector state unreadable (%s) — ignoring", e)
        return {}


def save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(p)


def set_state(cid: str, *, enabled: bool | None = None, mode: str | None = None,
              settings: dict | None = None) -> None:
    state = load_state()
    entry = state.setdefault(cid, {})
    if enabled is not None:
        entry["enabled"] = bool(enabled)
    if mode is not None:
        entry["mode"] = str(mode)
    if settings is not None:
        entry["settings"] = {str(k): str(v) for k, v in settings.items()}
    save_state(state)


def drop_state(cid: str) -> None:
    state = load_state()
    if state.pop(cid, None) is not None:
        save_state(state)


def refresh(registry) -> list[dict]:
    """Re-scan the connectors dir, apply state, sync the registry. Returns
    the admin status rows. Idempotent: connector-owned tools are swapped out
    wholesale, so a package edit/import/toggle takes effect immediately."""
    global _registered
    for name in _registered:
        registry.unregister(name)
    _registered = set()
    states = load_state()
    packages, errors = load_packages(paths.CUSTOM_CONN_DIR)
    rows = []
    for pkg in packages:
        st = states.get(pkg.id, {})
        enabled = bool(st.get("enabled", True))
        mode = st.get("mode") or pkg.default_mode
        if pkg.allows == "ro":
            mode = "ro"
        overrides = st.get("settings") or {}
        settings = {k: str(overrides.get(k, s["default"]))
                    for k, s in pkg.settings_schema.items()}
        n_live = n_write_live = 0
        tool_names: list[str] = []
        if enabled:
            try:
                tools = pkg.build_tools(settings, mode)
            except Exception as e:                  # bad settings value etc.
                log.error("connector '%s' failed to build: %s", pkg.id, e)
                errors.append(f"{pkg.id}: build failed — {e}")
                tools = []
            for tool in tools:
                if registry.register_instance(tool):
                    _registered.add(tool.name)
                    tool_names.append(tool.name)
                    n_live += 1
                    n_write_live += int(tool.write)
        rows.append({
            "id": pkg.id, "description": pkg.description, "enabled": enabled,
            "mode": mode, "allows": pkg.allows, "legacy": pkg.legacy,
            "tools_total": len(pkg.tool_specs), "tools_live": n_live,
            "writes_live": n_write_live, "tool_names": tool_names,
            "settings_schema": pkg.settings_schema, "settings": settings,
            "readme": pkg.readme, "source": str(pkg.source)})
    if errors:
        rows.append({"errors": errors})
    return rows
