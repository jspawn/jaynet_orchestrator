"""config/runtime.yaml loading + relative-path resolution.

The shipped runtime.yaml uses RELATIVE paths for everything under the
install root or the data dir, so a clone works anywhere:

  - install-tree paths (skills, chains, venvs, scripts) resolve against
    ORCH_HOME (runtime/paths.py HOME)
  - data paths (DBs, uploads, outputs, schedules, …) resolve against
    ORCH_DATA (paths.DATA)

Absolute values always pass through untouched, so existing machine-specific
configs keep working. The key list below is deliberately explicit — a new
path config key must opt in here (a generic "resolve everything that looks
like a path" walk would mis-fire on host-specific values like llama-server
binaries or model preset paths).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from runtime import paths

# Dotted key paths resolved against ORCH_HOME (install tree).
_HOME_KEYS = (
    "skills.dir",
    "chains.dir",
    "tools.ops.venv_bin",
    "tools.ops.project_root",
    "tools.code.python",
    "tools.serve.dispatcher",
    "tools.test.python",
    "tools.test.project_root",
)

# Dotted key paths resolved against ORCH_DATA (runtime state).
_DATA_KEYS = (
    "models.presets_db",
    "trace.db_path",
    "web.chats_db",
    "web.users_db",
    "web.uploads_dir",
    "web.outputs_dir",
    "web.projects_dir",
    "web.chat_scratch_dir",
    "web.wiki_dir",
    "tools.schedule.store",
    "tools.code.workdir",
    "tools.serve.state_dir",
    "tools.serve.default_cwd",
    "tools.rag.db_path",
    "tools.research.db_path",
    "tools.test.workdir_root",
)


def _anchor(config: dict, dotted: str, base: Path) -> None:
    """Resolve one allowlisted key in place; missing/non-string/absolute
    values are left alone."""
    d = config
    parts = dotted.split(".")
    for p in parts[:-1]:
        d = d.get(p)
        if not isinstance(d, dict):
            return
    key = parts[-1]
    value = d.get(key)
    if isinstance(value, str) and value and not Path(value).is_absolute():
        d[key] = str(base / value)


def resolve_paths(config: dict) -> dict:
    """Anchor the allowlisted relative paths in a parsed runtime.yaml
    (in place; returns the same dict). Absolute paths pass through."""
    if not isinstance(config, dict):
        return config
    for dotted in _HOME_KEYS:
        _anchor(config, dotted, paths.HOME)
    for dotted in _DATA_KEYS:
        _anchor(config, dotted, paths.DATA)
    # Managed-process commands: a relative executable token anchors to
    # ORCH_HOME ("scripts/start-model.sh brain" → "<ORCH_HOME>/scripts/…").
    # Bare program names (python, npx, …) stay PATH-relative.
    procs = config.get("processes")
    if isinstance(procs, dict):
        for entry in procs.values():
            if not isinstance(entry, dict):
                continue
            cmd = entry.get("command")
            if isinstance(cmd, str) and cmd and not cmd.startswith("/"):
                first, sep, rest = cmd.partition(" ")
                if "/" in first:
                    entry["command"] = str(paths.HOME / first) + sep + rest
    return config


def load_config(path: str | Path) -> dict:
    """Parse a runtime.yaml and resolve its relative paths (resolve_paths)."""
    with Path(path).open() as f:
        return resolve_paths(yaml.safe_load(f) or {})
