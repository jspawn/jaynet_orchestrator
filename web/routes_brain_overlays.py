"""Admin accept/history/rollback routes for versioned per-brain overlays."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import yaml
from fastapi import HTTPException, Request

from runtime import brain_overlays, harness_experiments, preset_store

_TWEAK_CAP = 5
_BRAIN_CLASSES = frozenset({
    "prompt-tweak", "skill-tweak", "tool-description", "config", "brain-overlay",
})
_FIX_META_PREFIX = re.compile(
    r"^\s*add\s+(?:a\s+|an\s+|the\s+)?(?:new\s+)?"
    r"(?:system[- ]?prompt\s+)?"
    r"(?:directive|instruction|rule|guideline|bullet|line|note)s?"
    r"(?:\s+to\s+the\s+(?:system[- ]?prompt|prompt))?"
    r"\s*[:\-–—]\s*",
    re.IGNORECASE)


def _clean_fix_text(fix: str) -> str:
    cleaned = _FIX_META_PREFIX.sub("", fix, count=1).strip()
    return cleaned or fix.strip()


def _overlay_store(runtime) -> brain_overlays.BrainOverlayStore:
    return brain_overlays.BrainOverlayStore(
        preset_store.db_path_for(runtime.config))


def _dated_lesson(prop: dict) -> str:
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"- {date} [{prop['test_id']}] {_clean_fix_text(prop['fix'])}"


def _lesson_count(text: str) -> int:
    return sum(1 for line in (text or "").splitlines() if line.startswith("- "))


def _ordinary_changes(runtime, prop: dict, current: dict) -> dict:
    cls = prop["classification"]
    if cls == "prompt-tweak":
        if _lesson_count(current["prompt_addition"]) >= _TWEAK_CAP:
            raise ValueError("brain prompt already carries 5 accepted tweaks; "
                             "roll back or consolidate them first")
        return {"prompt_addition": _dated_lesson(prop)}
    name = (prop.get("target") or "").strip()
    if cls == "skill-tweak":
        if name not in (getattr(runtime, "skills", None) or {}):
            raise ValueError(f"no skill '{name}' to tweak")
        previous = current["skill_additions"].get(name, "")
        if _lesson_count(previous) >= _TWEAK_CAP:
            raise ValueError(f"brain skill '{name}' already carries 5 tweaks")
        return {"skill_additions": {name: _dated_lesson(prop)}}
    if cls == "tool-description":
        known = {t.name for t in runtime.registry.all()}
        content = (prop.get("proposed_content") or "").strip()
        if name not in known:
            raise ValueError(f"no tool '{name}' to override")
        if not content:
            raise ValueError("tool-description proposal has no proposed_content")
        return {"tool_descriptions": {name: content}}
    if cls == "config":
        if name not in harness_experiments.CONFIG_WHITELIST:
            raise ValueError(f"config target '{name}' is not whitelisted")
        raw = (prop.get("proposed_content") or "").strip()
        if not raw:
            raise ValueError("config proposal has no proposed_content value")
        try:
            value = yaml.safe_load(raw)
            return brain_overlays.clean_overlay({"config": {name: value}})
        except (yaml.YAMLError, ValueError) as exc:
            raise ValueError(f"invalid brain config value: {exc}") from exc
    raise ValueError(f"classification '{cls}' is not brain-scoped")


def _experiment_content(prop: dict) -> dict:
    try:
        content = json.loads(prop.get("proposed_content") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid brain-overlay payload: {exc}") from exc
    if (not isinstance(content, dict) or content.get("schema") != 1
            or not isinstance(content.get("overlay"), dict)):
        raise ValueError("invalid brain-overlay payload schema")
    if int(content.get("base_version") or 0) != int(
            prop.get("base_overlay_version") or 0):
        raise ValueError("brain-overlay base version does not match the proposal")
    return content


def _changes(runtime, prop: dict, current: dict) -> dict:
    if prop["classification"] != "brain-overlay":
        return _ordinary_changes(runtime, prop, current)
    content = _experiment_content(prop)
    overlay = harness_experiments.validate_overlay(
        content["overlay"], runtime, allow_empty=False)
    return brain_overlays.from_experiment(overlay)


def apply_proposal(runtime, prop: dict) -> dict:
    brain = str(prop.get("brain_preset") or "")
    if not brain:
        raise ValueError("proposal has no brain preset identity")
    db_path = preset_store.db_path_for(runtime.config)
    if preset_store.PresetStore(db_path).get(brain) is None:
        raise ValueError(f"brain preset '{brain}' no longer exists")
    store = _overlay_store(runtime)
    current = store.active(brain)
    changes = _changes(runtime, prop, current["overlay"])
    experiment_id = (_experiment_content(prop).get("experiment_id")
                     if prop["classification"] == "brain-overlay" else None)
    version = store.apply(
        brain, changes, base_version=int(prop.get("base_overlay_version") or 0),
        proposal_id=int(prop["id"]), experiment_id=experiment_id)
    if not preset_store.load_into_config(runtime.config):
        raise ValueError("overlay was stored but hot reload failed; retry accept")
    return version


def _known_brain(runtime, name: str) -> None:
    if preset_store.PresetStore(
            preset_store.db_path_for(runtime.config)).get(name) is None:
        raise HTTPException(status_code=404, detail=f"no brain preset '{name}'")


def register(app, runtime, store_factory, legacy_accept) -> None:
    """Register routes; `legacy_accept` preserves the old global code path."""
    @app.post("/api/admin/evals/proposals/{pid}/accept")
    async def eval_proposal_accept(pid: int):
        store = store_factory()
        try:
            prop = store.get_proposal(pid)
            if prop is None:
                raise HTTPException(status_code=404, detail=f"no proposal #{pid}")
        finally:
            store.close()
        cls = prop["classification"]
        if prop.get("brain_preset") and cls in _BRAIN_CLASSES:
            try:
                version = apply_proposal(runtime, prop)
            except brain_overlays.StaleOverlayError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                return {"proposal": prop, "applied": "none", "path": None,
                        "note": str(exc)}
            store = store_factory()
            try:
                prop = store.set_proposal_status(pid, "accepted")
            finally:
                store.close()
            return {"proposal": prop, "applied": "brain-overlay",
                    "path": f"{version['preset']}@v{version['version']}",
                    "overlay_version": version}
        return legacy_accept(prop)

    @app.get("/api/admin/brain-overlays")
    async def brain_overlay_list():
        store = _overlay_store(runtime)
        presets = ((runtime.config.get("models") or {}).get("presets") or {})
        overlays = [store.active(name) for name, preset in sorted(presets.items())
                    if not preset.get("archived")]
        return {"overlays": overlays}

    @app.get("/api/admin/brain-overlays/{preset}")
    async def brain_overlay_history(preset: str):
        _known_brain(runtime, preset)
        store = _overlay_store(runtime)
        versions = store.history(preset)
        baseline = store.get(preset, 0)
        assert baseline is not None
        versions.append(baseline)
        return {"active": store.active(preset), "versions": versions}

    @app.post("/api/admin/brain-overlays/{preset}/rollback")
    async def brain_overlay_rollback(preset: str, request: Request):
        _known_brain(runtime, preset)
        try:
            body = await request.json()
            source = int(body.get("version"))
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400,
                                detail="body must contain an integer version") from exc
        try:
            version = _overlay_store(runtime).rollback(preset, source)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not preset_store.load_into_config(runtime.config):
            raise HTTPException(status_code=500,
                                detail="rollback stored but hot reload failed")
        return {"ok": True, "overlay_version": version,
                "path": f"{preset}@v{version['version']}"}
