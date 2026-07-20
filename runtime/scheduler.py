"""Scheduled prompts — one-shot reminders and recurring agent runs.

The store is deliberately a JSON file: a handful of entries, human-inspectable,
atomic writes (tmp + replace). The web server ticks it from an asyncio task
(web/server.py) and fires due entries through the normal runtime; entries are
owner-scoped (the web user who created them).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

_REL_RE = re.compile(r"^\+(\d+)([mhdw])$")
_EVERY_RE = re.compile(r"^(\d+)([mhdw])$")
_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_every(value: str) -> int:
    """'30m'/'2h'/'1d'/'1w' -> seconds."""
    m = _EVERY_RE.match((value or "").strip().lower())
    if not m:
        raise ValueError(f"invalid interval {value!r} (use e.g. '30m', '2h', '1d', '1w')")
    return int(m.group(1)) * _UNIT[m.group(2)]


def parse_when(value: str, now: float | None = None) -> float:
    """'+30m' relative or an ISO-8601 datetime -> epoch seconds."""
    now = now or time.time()
    v = (value or "").strip()
    m = _REL_RE.match(v.lower())
    if m:
        return now + int(m.group(1)) * _UNIT[m.group(2)]
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"invalid time {value!r} (use '+30m' or ISO-8601)") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()                  # naive -> local timezone
    return dt.timestamp()


class ScheduleStore:
    def __init__(self, path: str):
        self.path = Path(path)

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=1))
        tmp.replace(self.path)

    def add(self, entry: dict) -> dict:
        entries = self._load()
        entry = dict(entry)
        entry["id"] = uuid.uuid4().hex[:8]
        entry.setdefault("enabled", True)
        entry.setdefault("fire_count", 0)
        entries.append(entry)
        self._save(entries)
        return entry

    def list(self, owner: str | None = None) -> list[dict]:
        return [e for e in self._load() if owner is None or e.get("owner") == owner]

    def remove(self, entry_id: str, owner: str | None = None) -> bool:
        entries = self._load()
        keep = [e for e in entries
                if not (e.get("id") == entry_id
                        and (owner is None or e.get("owner") == owner))]
        if len(keep) == len(entries):
            return False
        self._save(keep)
        return True

    def due(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        return [e for e in self._load()
                if e.get("enabled") and e.get("next_fire")
                and float(e["next_fire"]) <= now]

    def mark_fired(self, entry_id: str, now: float | None = None) -> None:
        """Record a firing: one-shots disable, recurrences advance. Advancement
        is anchored on the SCHEDULED time (not now), so a late tick doesn't
        drift the cadence; skipped-over intervals collapse to the next one."""
        now = now or time.time()
        entries = self._load()
        for e in entries:
            if e.get("id") != entry_id:
                continue
            e["fire_count"] = int(e.get("fire_count", 0)) + 1
            e["last_fired"] = now
            if e.get("kind") == "every" and e.get("every_s"):
                nxt = float(e["next_fire"]) + int(e["every_s"])
                while nxt <= now:
                    nxt += int(e["every_s"])
                e["next_fire"] = nxt
            else:
                e["enabled"] = False
        self._save(entries)
