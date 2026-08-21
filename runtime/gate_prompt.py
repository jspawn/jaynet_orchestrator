"""Gate prompt layering — shipped default + live overlay.

The shipped prompt (`prompts/orchestrator-gate.md`, configured via
orchestrator.system_prompt) stays git-managed and pristine: deploys update
it, and its diff is the review trail for changes WE ship. Live edits — the
admin Prompt tab, accepted eval proposals — write the overlay at
$ORCH_DATA/custom/<prompt-name>; when it exists it wins. This keeps
/srv/orchestrator clean (no pull conflicts from live edits) and makes "what
is live actually running" a diff away.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Section marker under which accepted eval prompt-tweak proposals collect
# (see web/routes_eval.py's _apply_prompt_tweak). Consolidation folds them
# back into the prose and drops the section.
PROPOSALS_MARKER = "<!-- eval-proposals -->"


def count_tweak_bullets(text: str) -> int:
    """Accepted eval tweak bullets currently bolted onto the prompt."""
    if PROPOSALS_MARKER not in (text or ""):
        return 0
    section = text.split(PROPOSALS_MARKER, 1)[1]
    return sum(1 for ln in section.splitlines() if ln.startswith("- "))


def shipped_path(config: dict, config_path: Path) -> Path:
    orch_root = Path(config_path).parent.parent
    return orch_root / config["orchestrator"]["system_prompt"]


def overlay_path(config: dict) -> Path:
    from runtime import paths
    name = Path(config["orchestrator"]["system_prompt"]).name
    return paths.CUSTOM_DIR / name


def load(config: dict, config_path: Path) -> tuple[str, str]:
    """(content, layer) — layer is "custom" (overlay) or "shipped"."""
    overlay = overlay_path(config)
    if overlay.is_file():
        log.info("gate prompt: using live overlay %s", overlay)
        return overlay.read_text(encoding="utf-8", errors="replace"), "custom"
    return (shipped_path(config, config_path)
            .read_text(encoding="utf-8", errors="replace")), "shipped"


def save_overlay(config: dict, content: str) -> Path:
    p = overlay_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    # tmp + replace (like cloud_store.write_rendered): a crash mid-write must
    # never leave a truncated live prompt for load() to serve.
    tmp = p.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(p)
    return p


def revert(config: dict) -> bool:
    """Delete the overlay (back to the shipped prompt). False if none."""
    p = overlay_path(config)
    if p.is_file():
        p.unlink()
        return True
    return False
