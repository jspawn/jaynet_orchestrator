"""One-line help strings for admin → Config (shipped: config/config-help.yaml).

The file carries two maps: `exact` (dotpath → text) and `patterns`
(fnmatch glob → text, e.g. "costs.*.input"). Exact wins; the first
matching pattern in file order wins after that. Missing file → empty
help, the UI just shows no hints. tests/test_config_help.py enforces
that every leaf key of the shipped runtime.yaml resolves to a string.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import yaml


def load_help(config_path: str | Path) -> dict:
    """{"exact": {...}, "patterns": {...}} from config-help.yaml next to
    the runtime config; empty maps when the file is absent."""
    p = Path(config_path).parent / "config-help.yaml"
    if not p.is_file():
        return {"exact": {}, "patterns": {}}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {"exact": data.get("exact") or {},
            "patterns": data.get("patterns") or {}}


def match(key: str, help_map: dict) -> str:
    """Help text for one flat config key; "" when undocumented."""
    exact = help_map.get("exact") or {}
    if key in exact:
        return exact[key]
    for pat, text in (help_map.get("patterns") or {}).items():
        if fnmatch.fnmatchcase(key, pat):
            return text
    return ""
