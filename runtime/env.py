"""Env-var access with the JAYNET_/ORCH_ dual read.

New installs set ``JAYNET_*`` (see example_configs/jaynet.env.example);
``ORCH_*`` is still honoured as a fallback so existing env files keep
working untouched. Docs show the JAYNET_* names only.

Internal-only contract names that never appear in the env file
(``ORCH_EXEC_OUT`` for code.run snippets) stay as they are.
"""

from __future__ import annotations

import os


def env(name: str, default=None):
    """Read an ``ORCH_``-style key: ``JAYNET_<suffix>`` wins, ``ORCH_<suffix>``
    falls back. ``default`` when neither is set."""
    suffix = name[5:] if name.startswith("ORCH_") else name
    v = os.environ.get("JAYNET_" + suffix)
    if v is None:
        v = os.environ.get("ORCH_" + suffix)
    return default if v is None else v
