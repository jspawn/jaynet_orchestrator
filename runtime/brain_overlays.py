"""Append-only, per-preset harness overlays in ``presets.db``.

An overlay is a complete effective snapshot, not a diff. Accepting a proposal
merges its changes into the active snapshot and appends a version; rollback
appends a copy of an older snapshot. Version 0 is the implicit empty baseline.
The store is stdlib-only because the preset catalog is also used by the model
launcher outside the web environment.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS preset_overlay_versions(
  preset TEXT NOT NULL,
  version INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 0,
  payload TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  base_version INTEGER NOT NULL DEFAULT 0,
  source_version INTEGER,
  proposal_id INTEGER UNIQUE,
  experiment_id TEXT,
  created REAL NOT NULL,
  PRIMARY KEY(preset, version));
CREATE UNIQUE INDEX IF NOT EXISTS idx_preset_overlay_active
  ON preset_overlay_versions(preset) WHERE active=1;
CREATE INDEX IF NOT EXISTS idx_preset_overlay_history
  ON preset_overlay_versions(preset, version DESC);
"""

_KEYS = ("prompt_addition", "skill_additions", "tool_descriptions", "config")
CONFIG_WHITELIST = frozenset({
    "budgets.max_iterations", "budgets.max_wall_clock_s",
    "budgets.max_cost_usd", "budgets.max_total_tokens",
    "loop_guard.max_rejections", "architect.threshold",
    "eval.max_cost_usd", "eval.suite_max_cost_usd",
    "eval.adaptive_max_turns",
})


class StaleOverlayError(ValueError):
    """The proposal was generated against a superseded overlay version."""


def empty_overlay() -> dict[str, Any]:
    return {"prompt_addition": "", "skill_additions": {},
            "tool_descriptions": {}, "config": {}}


def clean_overlay(raw: Any) -> dict[str, Any]:
    """Validate the canonical stored shape without resolving runtime names."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - set(_KEYS):
        raise ValueError("brain overlay has unknown fields or is not an object")
    out = empty_overlay()
    prompt = raw.get("prompt_addition") or ""
    if not isinstance(prompt, str) or len(prompt) > 30000:
        raise ValueError("brain overlay prompt_addition must be text <= 30000 chars")
    out["prompt_addition"] = prompt.strip()
    for key, limit in (("skill_additions", 100), ("tool_descriptions", 100),
                       ("config", 50)):
        value = raw.get(key) or {}
        if not isinstance(value, dict) or len(value) > limit:
            raise ValueError(f"brain overlay {key} must be an object")
        cleaned = {}
        for name, item in value.items():
            if not isinstance(name, str) or not name.strip() or len(name) > 100:
                raise ValueError(f"brain overlay {key} has an invalid name")
            if key == "config":
                if name not in CONFIG_WHITELIST:
                    raise ValueError(f"brain overlay config key '{name}' is not allowed")
                if (isinstance(item, bool) or not isinstance(item, (int, float))
                        or not math.isfinite(item)):
                    raise ValueError("brain overlay config values must be finite numbers")
            elif not isinstance(item, str) or not item.strip() or len(item) > 10000:
                raise ValueError(f"brain overlay {key} values must be nonempty text")
            cleaned[name.strip()] = item
        out[key] = cleaned
    return out


def overlay_hash(overlay: dict) -> str:
    blob = json.dumps(clean_overlay(overlay), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def from_experiment(raw: dict | None) -> dict[str, Any]:
    """Convert the singular experiment edit shape to canonical merge changes."""
    raw = raw or {}
    out = empty_overlay()
    if raw.get("prompt_addition"):
        out["prompt_addition"] = raw["prompt_addition"]
    if raw.get("skill_addition"):
        edit = raw["skill_addition"]
        out["skill_additions"][edit["name"]] = edit["content"]
    if raw.get("tool_description"):
        edit = raw["tool_description"]
        out["tool_descriptions"][edit["name"]] = edit["content"]
    if raw.get("config"):
        edit = raw["config"]
        out["config"][edit["key"]] = edit["value"]
    return clean_overlay(out)


def merge_overlay(base: dict, changes: dict) -> dict[str, Any]:
    """Append lessons, replace exact tool/config targets, return a new snapshot."""
    out = clean_overlay(copy.deepcopy(base))
    changes = clean_overlay(changes)
    if changes["prompt_addition"]:
        out["prompt_addition"] = "\n\n".join(
            part for part in (out["prompt_addition"], changes["prompt_addition"])
            if part)
    for key in ("skill_additions",):
        for name, content in changes[key].items():
            previous = out[key].get(name, "")
            out[key][name] = "\n\n".join(x for x in (previous, content) if x)
    for key in ("tool_descriptions", "config"):
        out[key].update(changes[key])
    return clean_overlay(out)


def config_with_overlay(config: dict, overlay: dict) -> dict:
    """Return a run-local config view with canonical overlay values applied."""
    values = clean_overlay(overlay)["config"]
    if not values:
        return config
    out = dict(config)
    for dotted, value in values.items():
        section, key = dotted.split(".", 1)
        out[section] = {**(config.get(section) or {}), key: value}
    return out


def run_overrides_with_overlay(run_overrides: dict | None,
                               overlay: dict) -> dict:
    """Layer persistent named edits below explicit per-run edits."""
    overlay = clean_overlay(overlay)
    out = dict(run_overrides or {})
    for key in ("skill_additions", "tool_descriptions"):
        out[key] = {**overlay[key], **((run_overrides or {}).get(key) or {})}
    return out


def prepare_run(config: dict, model: str, run_overrides: dict | None,
                extra_system: str | None, patch_run_config) -> tuple[dict, dict, str | None]:
    """Compose persistent and explicit per-run state outside the agent loop."""
    from runtime.preset_store import brain_overlay_for
    overlay = brain_overlay_for(config, model)["overlay"]
    overrides = run_overrides_with_overlay(run_overrides, overlay)
    run_config = config_with_overlay(config, overlay)
    run_config = patch_run_config(run_config, overrides.get("tools_patch"),
                                  overrides.get("config_patch"))
    if overlay["prompt_addition"]:
        extra_system = "\n\n".join(
            x for x in (overlay["prompt_addition"], extra_system or "") if x)
    return overrides, run_config, extra_system


def experiment_run_overrides(run_overrides: dict, experiment: dict) -> dict:
    """Translate the singular experiment edits into internal run overrides."""
    out = dict(run_overrides)
    if experiment.get("skill_addition"):
        edit = experiment["skill_addition"]
        out["skill_additions"] = {edit["name"]: edit["content"]}
    if experiment.get("tool_description"):
        edit = experiment["tool_description"]
        out["tool_descriptions"] = {edit["name"]: edit["content"]}
    if experiment.get("config"):
        edit = experiment["config"]
        section, key = edit["key"].split(".", 1)
        patch = dict(out.get("config_patch") or {})
        patch[section] = {**(patch.get(section) or {}), key: edit["value"]}
        out["config_patch"] = patch
    return out


class BrainOverlayStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure(self) -> None:
        with self._conn() as conn:
            conn.executescript("PRAGMA journal_mode=WAL;\n"
                               "PRAGMA synchronous=NORMAL;\n"
                               "PRAGMA busy_timeout=10000;")
            conn.executescript(SCHEMA)

    @staticmethod
    def _row(row: sqlite3.Row | None, preset: str) -> dict:
        if row is None:
            overlay = empty_overlay()
            return {"preset": preset, "version": 0, "active": True,
                    "overlay": overlay, "content_hash": overlay_hash(overlay),
                    "base_version": 0, "source_version": None,
                    "proposal_id": None, "experiment_id": None, "created": None}
        out = dict(row)
        out["overlay"] = clean_overlay(json.loads(out.pop("payload")))
        out["active"] = bool(out["active"])
        return out

    def active(self, preset: str) -> dict:
        self.ensure()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM preset_overlay_versions WHERE preset=? AND active=1",
                (preset,)).fetchone()
        return self._row(row, preset)

    def get(self, preset: str, version: int) -> dict | None:
        if int(version) == 0:
            baseline = self._row(None, preset)
            baseline["active"] = self.active(preset)["version"] == 0
            return baseline
        self.ensure()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM preset_overlay_versions WHERE preset=? AND version=?",
                (preset, int(version))).fetchone()
        return self._row(row, preset) if row else None

    def history(self, preset: str, limit: int = 100) -> list[dict]:
        self.ensure()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM preset_overlay_versions WHERE preset=? "
                "ORDER BY version DESC LIMIT ?",
                (preset, max(1, min(int(limit), 500)))).fetchall()
        return [self._row(row, preset) for row in rows]

    def apply(self, preset: str, changes: dict, *, base_version: int,
              proposal_id: int | None = None,
              experiment_id: str | None = None) -> dict:
        changes = clean_overlay(changes)
        self.ensure()
        with self._conn() as conn:
            if proposal_id is not None:
                prior = conn.execute(
                    "SELECT * FROM preset_overlay_versions WHERE proposal_id=?",
                    (proposal_id,)).fetchone()
                if prior:
                    return self._row(prior, preset)
            row = conn.execute(
                "SELECT * FROM preset_overlay_versions WHERE preset=? AND active=1",
                (preset,)).fetchone()
            current = self._row(row, preset)
            if current["version"] != int(base_version):
                raise StaleOverlayError(
                    f"proposal targets {preset}@v{base_version}, but the active "
                    f"overlay is v{current['version']}; rerun the experiment")
            merged = merge_overlay(current["overlay"], changes)
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM preset_overlay_versions "
                "WHERE preset=?", (preset,)).fetchone()[0]
            conn.execute(
                "UPDATE preset_overlay_versions SET active=0 WHERE preset=?",
                (preset,))
            conn.execute(
                "INSERT INTO preset_overlay_versions (preset,version,active,payload,"
                "content_hash,base_version,source_version,proposal_id,experiment_id,"
                "created) VALUES (?,?,1,?,?,?,NULL,?,?,?)",
                (preset, version,
                 json.dumps(merged, sort_keys=True, ensure_ascii=False),
                 overlay_hash(merged), int(base_version), proposal_id,
                 experiment_id, time.time()))
            inserted = conn.execute(
                "SELECT * FROM preset_overlay_versions WHERE preset=? AND version=?",
                (preset, version)).fetchone()
        return self._row(inserted, preset)

    def rollback(self, preset: str, source_version: int) -> dict:
        self.ensure()
        with self._conn() as conn:
            source = conn.execute(
                "SELECT * FROM preset_overlay_versions WHERE preset=? AND version=?",
                (preset, int(source_version))).fetchone()
            if int(source_version) != 0 and source is None:
                raise ValueError(f"no {preset} overlay version {source_version}")
            snapshot = (clean_overlay(json.loads(source["payload"])) if source
                        else empty_overlay())
            current_row = conn.execute(
                "SELECT * FROM preset_overlay_versions WHERE preset=? AND active=1",
                (preset,)).fetchone()
            current = self._row(current_row, preset)
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM preset_overlay_versions "
                "WHERE preset=?", (preset,)).fetchone()[0]
            conn.execute(
                "UPDATE preset_overlay_versions SET active=0 WHERE preset=?",
                (preset,))
            conn.execute(
                "INSERT INTO preset_overlay_versions (preset,version,active,payload,"
                "content_hash,base_version,source_version,proposal_id,experiment_id,"
                "created) VALUES (?,?,1,?,?,?,?,NULL,NULL,?)",
                (preset, version,
                 json.dumps(snapshot, sort_keys=True, ensure_ascii=False),
                 overlay_hash(snapshot), current["version"], int(source_version),
                 time.time()))
            inserted = conn.execute(
                "SELECT * FROM preset_overlay_versions WHERE preset=? AND version=?",
                (preset, version)).fetchone()
        return self._row(inserted, preset)
