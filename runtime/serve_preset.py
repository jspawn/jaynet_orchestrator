"""Read model presets (start-model.sh format).

A preset is a flat KEY=VALUE file that fully describes one servable model: its
weights path, optional vision projector (MMPROJ), served alias, and all
llama-server flags. The orchestrator reads it to learn what the brain
currently is — most importantly, whether it can see images (an MMPROJ is
loaded) — so it forwards image attachments only when that holds.
"""
from __future__ import annotations

import os
from pathlib import Path


def parse_preset(path: str | Path) -> dict[str, str]:
    """Parse a KEY=VALUE preset file into a dict. Missing file -> {}.

    Values go through env-var (~ and $VAR) expansion: presets reference
    models as $ORCH_MODELS/… so the same files work on any host."""
    data: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return data
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        data[key.strip()] = os.path.expanduser(os.path.expandvars(val.strip()))
    return data


def preset_info(path: str | Path) -> dict:
    """Normalized view of a preset for the orchestrator.

    `vision` is True iff the preset loads a vision projector (MMPROJ non-empty),
    which is exactly the condition under which llama-server can accept images.
    """
    d = parse_preset(path)
    model_path = d.get("MODEL_PATH", "").strip()
    mmproj = d.get("MMPROJ", "").strip()
    return {
        "model_path": model_path,
        "model": Path(model_path).name if model_path else "",
        "alias": d.get("ALIAS", "").strip(),
        "mmproj": mmproj,
        "vision": bool(mmproj),
        "ctx_size": d.get("CTX_SIZE", "").strip(),
        "host": d.get("HOST", "").strip(),
        "port": d.get("PORT", "").strip(),
        "backend": d.get("BACKEND", "").strip(),
    }
