"""Scheduled prompts — one-shot reminders and recurring agent runs.

The store is deliberately a JSON file: a handful of entries, human-inspectable,
atomic writes (tmp + replace). The web server ticks it from an asyncio task
(web/server.py) and fires due entries through the normal runtime; entries are
owner-scoped (the web user who created them).
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

_REL_RE = re.compile(r"^\+(\d+)([mhdw])$")
_EVERY_RE = re.compile(r"^(\d+)([mhdw])$")
_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 604800}

# Serializes load→mutate→save across all store instances in this process —
# the store is instantiated per call (tools/schedule) and once per web server
# (web/routes_procs), so an instance attribute would not serialize anything.
# Methods are sync and called from async contexts; critical sections are a
# tiny JSON read+write, so a plain threading.Lock is fine.
_STORE_LOCK = threading.Lock()


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


def _cron_field(spec: str, lo: int, hi: int) -> set[int]:
    """One cron field -> its set of allowed values. Supports `*`, `*/n`,
    `a`, `a-b`, `a-b/n` and comma lists of those. A stepped value without a
    range (`5/10`) means `5-hi/10`, standard cron semantics."""
    out: set[int] = set()
    for part in spec.split(","):
        had_step = "/" in part
        step = 1
        if had_step:
            part, _, st = part.partition("/")
            step = int(st)                    # ValueError on junk -> caller
            if step < 1:
                raise ValueError(f"invalid step in cron field {spec!r}")
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a_s, _, b_s = part.partition("-")
            a, b = int(a_s), int(b_s)
        elif had_step:
            a, b = int(part), hi
        else:
            a = b = int(part)
        if a < lo or b > hi or a > b:
            raise ValueError(f"cron value out of range ({lo}-{hi}): {part!r}")
        out.update(range(a, b + 1, step))
    if not out:
        raise ValueError(f"empty cron field {spec!r}")
    return out


def parse_cron(expr: str, now: float) -> float:
    """Next fire time (epoch seconds) for a 5-field cron expression
    `minute hour dom month dow`, matched in LOCAL time. Classic cron
    semantics: when BOTH dom and dow are restricted (don't start with `*`),
    a day matches if EITHER matches; otherwise both must match. dow is
    0-6 = Sunday-Saturday, with 7 also accepted as Sunday.

    Fields are wall-clock local, so across a DST spring-forward a nonexistent
    local minute is skipped (mktime normalizes it to a real one — the
    round-trip check below refuses the shift) and at fall-back a repeated
    minute fires on its first occurrence only. Raises ValueError on a
    malformed expression or one with no fire time within ~4 years (e.g.
    '0 0 30 2 *').

    The scan is DAY-by-day, then the day's candidate times — a
    minute-by-minute scan took ~2s on impossible dates and ran under the
    store lock in mark_fired, stalling the web server (code audit
    2026-10-14). Worst case is now ~1,461 cheap date checks."""
    fields = (expr or "").split()
    if len(fields) != 5:
        raise ValueError(f"invalid cron {expr!r} (want 5 fields: "
                         "'minute hour day-of-month month day-of-week')")
    minutes = _cron_field(fields[0], 0, 59)
    hours = _cron_field(fields[1], 0, 23)
    doms = _cron_field(fields[2], 1, 31)
    months = _cron_field(fields[3], 1, 12)
    # Cron dow is Sunday=0 (7 aliases to 0); date.weekday() is Monday=0, so
    # cron_dow = (weekday() + 1) % 7.
    dows = {0 if d == 7 else d for d in _cron_field(fields[4], 0, 7)}
    either_day = not fields[2].startswith("*") and not fields[4].startswith("*")
    lt = time.localtime(now)
    day = date(lt.tm_year, lt.tm_mon, lt.tm_mday)
    deadline = day + timedelta(days=4 * 366)       # bounded scan (~4 years)
    times = sorted((h, m) for h in hours for m in minutes)
    while day <= deadline:
        dom_ok, dow_ok = day.day in doms, (day.weekday() + 1) % 7 in dows
        day_ok = (dom_ok or dow_ok) if either_day else (dom_ok and dow_ok)
        if day_ok and day.month in months:
            for h, m in times:
                t = time.mktime((day.year, day.month, day.day,
                                 h, m, 0, 0, 0, -1))
                back = time.localtime(t)
                # A DST-nonexistent local minute normalizes to a shifted real
                # one — skip it (documented semantics), don't fire shifted.
                if (back.tm_hour, back.tm_min) != (h, m):
                    continue
                if t > now:
                    return float(t)
        day += timedelta(days=1)
    raise ValueError(f"cron {expr!r} has no fire time within ~4 years")


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
        with _STORE_LOCK:
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
        with _STORE_LOCK:
            entries = self._load()
            keep = [e for e in entries
                    if not (e.get("id") == entry_id
                            and (owner is None or e.get("owner") == owner))]
            if len(keep) == len(entries):
                return False
            self._save(keep)
        return True

    def remove_owner(self, owner: str) -> int:
        """Delete every entry owned by `owner` (account deletion — the runs
        must not keep firing for a user that no longer exists)."""
        with _STORE_LOCK:
            entries = self._load()
            keep = [e for e in entries if e.get("owner") != owner]
            if len(keep) != len(entries):
                self._save(keep)
            return len(entries) - len(keep)

    def due(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        out = []
        for e in self._load():
            try:
                if (e.get("enabled") and e.get("next_fire")
                        and float(e["next_fire"]) <= now):
                    out.append(e)
            except (TypeError, ValueError):
                continue   # quarantine a malformed hand-edit, don't stall the rest
        return out

    def mark_fired(self, entry_id: str, now: float | None = None) -> None:
        """Record a firing: one-shots disable, recurrences advance. 'every'
        advancement is anchored on the SCHEDULED time (not now), so a late
        tick doesn't drift the cadence; skipped-over intervals collapse to
        the next one. 'cron' recomputes from now (absolute wall-clock
        schedule — nothing to drift); a broken cron expression disables the
        entry instead of stalling the store."""
        now = now or time.time()
        with _STORE_LOCK:
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
                elif e.get("kind") == "cron" and e.get("cron"):
                    try:
                        e["next_fire"] = parse_cron(e["cron"], now)
                    except ValueError:
                        e["enabled"] = False
                else:
                    e["enabled"] = False
            self._save(entries)

    def set_enabled(self, entry_id: str, enabled: bool) -> bool:
        """Pause/resume an entry. False when no entry has that id."""
        with _STORE_LOCK:
            entries = self._load()
            for e in entries:
                if e.get("id") == entry_id:
                    e["enabled"] = bool(enabled)
                    self._save(entries)
                    return True
        return False

    def set_status(self, entry_id: str, status: str | None) -> bool:
        """Record the last run's outcome on the entry (shown in the admin
        Schedules tab). False when no entry has that id."""
        with _STORE_LOCK:
            entries = self._load()
            for e in entries:
                if e.get("id") == entry_id:
                    e["last_status"] = status
                    self._save(entries)
                    return True
        return False
