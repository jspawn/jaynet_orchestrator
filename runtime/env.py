"""Env-var access with the JAYNET_/ORCH_ dual read.

New installs set ``JAYNET_*`` (see example_configs/jaynet.env.example);
``ORCH_*`` is still honoured as a fallback so existing env files keep
working untouched. Docs show the JAYNET_* names only.

Internal-only contract names that never appear in the env file
(``ORCH_EXEC_OUT`` for code.run snippets) stay as they are.
"""

from __future__ import annotations

import os
from pathlib import Path


def env(name: str, default=None):
    """Read an ``ORCH_``-style key: ``JAYNET_<suffix>`` wins, ``ORCH_<suffix>``
    falls back. ``default`` when neither is set."""
    suffix = name[5:] if name.startswith("ORCH_") else name
    v = os.environ.get("JAYNET_" + suffix)
    if v is None:
        v = os.environ.get("ORCH_" + suffix)
    return default if v is None else v


def load_env_file(path=None):
    """Load the install's env file into ``os.environ`` (defaults only — a var
    already set in the process env wins). The systemd units get these vars via
    ``EnvironmentFile``; CLI entry points (``scripts/orch``) call this so they
    resolve the same JAYNET_HOME / JAYNET_DATA / ports. Returns the Path used,
    or None when no env file exists."""
    candidates = [Path(path)] if path else [
        Path.home() / ".config" / "jaynet.env",
        Path.home() / ".config" / "orchestrator.env",  # legacy name
    ]
    for f in candidates:
        if not f.is_file():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, val)
        return f
    return None
