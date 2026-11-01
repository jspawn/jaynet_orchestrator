"""Immutable observed-source events and deterministic accept decisions.

Phase 2 is observation only: these rows never authorize an agent run by
 themselves. The schedule JSON remains the mandate; every decision carries the
rule/prompt versions and a hash of the mandate it was evaluated against.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from web.store import ensure_private_store

_HISTORY_LIMIT = 50
_MANDATE_KEYS = ("source", "owner", "project_id", "rule_version",
                 "prompt_version", "tool_scope", "budget", "prompt", "query")


def mandate_hash(entry: dict) -> str:
    snapshot = {key: entry.get(key) for key in _MANDATE_KEYS}
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _text(value, limit: int) -> str:
    return str(value or "")[:limit]


def _paper_payload(paper: dict) -> tuple[str, str]:
    external_id = _text(paper.get("id"), 200).strip()
    if not external_id:
        raise ValueError("observed arXiv record has no id")
    payload = {
        "title": _text(paper.get("title"), 1000),
        "authors": [_text(a, 100) for a in (paper.get("authors") or [])[:50]],
        "published": _text(paper.get("published"), 32),
        "updated": _text(paper.get("updated"), 32),
        "summary": _text(paper.get("summary"), 2000),
        "primary_category": _text(paper.get("primary_category"), 100),
        "categories": [_text(c, 100) for c in (paper.get("categories") or [])[:20]],
        "comment": _text(paper.get("comment"), 500),
        "doi": _text(paper.get("doi"), 200),
        "pdf_url": _text(paper.get("pdf_url"), 500),
        "abs_url": _text(paper.get("abs_url"), 500),
    }
    return external_id, json.dumps(payload, ensure_ascii=False)


class TriggerStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trigger_events(
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    owner TEXT,
                    external_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    UNIQUE(source_id, external_id)
                );
                CREATE INDEX IF NOT EXISTS idx_trigger_owner
                    ON trigger_events(owner);
                CREATE INDEX IF NOT EXISTS idx_trigger_source
                    ON trigger_events(source_id, observed_at);
                CREATE TABLE IF NOT EXISTS trigger_decisions(
                    id TEXT PRIMARY KEY,
                    trigger_id TEXT NOT NULL UNIQUE
                        REFERENCES trigger_events(id) ON DELETE CASCADE,
                    outcome TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    prompt_version INTEGER NOT NULL,
                    mandate_hash TEXT NOT NULL,
                    scheduled_for REAL NOT NULL,
                    decided_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_decision_trigger
                    ON trigger_decisions(trigger_id);
            """)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_private_store(self.db_path)
        return conn

    def record_poll(self, entry: dict, papers: list[dict],
                    scheduled_for: float, now: float | None = None) -> dict:
        """Atomically record unseen versioned IDs and their one decision.

        The first successful non-empty poll is a baseline, not a backlog of
        accepted work. Replaying a poll after a crash is a no-op.
        """
        ts = time.time() if now is None else now
        sid = str(entry["id"])
        outcome = "baseline"
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM trigger_events WHERE source_id=? LIMIT 1",
                (sid,)).fetchone()
            if exists:
                outcome = "would_accept"
            inserted, duplicates, seen = 0, 0, set()
            for paper in papers:
                external_id, payload = _paper_payload(paper)
                if external_id in seen:
                    duplicates += 1
                    continue
                seen.add(external_id)
                trigger_id = uuid.uuid4().hex
                cur = conn.execute(
                    "INSERT OR IGNORE INTO trigger_events(id,source_id,source,"
                    "owner,external_id,payload_json,observed_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (trigger_id, sid, entry["source"], entry.get("owner"),
                     external_id, payload, ts))
                if cur.rowcount != 1:
                    duplicates += 1
                    continue
                reason = ("initial successful non-empty poll" if outcome == "baseline"
                          else "new versioned arXiv id for active mandate")
                conn.execute(
                    "INSERT INTO trigger_decisions(id,trigger_id,outcome,reason,"
                    "rule_version,prompt_version,mandate_hash,scheduled_for,"
                    "decided_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, trigger_id, outcome, reason,
                     entry["rule_version"], int(entry["prompt_version"]),
                     mandate_hash(entry), float(scheduled_for), ts))
                inserted += 1
            conn.commit()
            return {"recorded": inserted, "duplicates": duplicates,
                    "outcome": outcome if inserted else None}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def history(self, schedule_id: str, limit: int = _HISTORY_LIMIT) -> list[dict]:
        limit = max(1, min(int(limit), _HISTORY_LIMIT))
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT d.id AS decision_id,d.outcome,d.reason,d.rule_version,"
                "d.prompt_version,d.mandate_hash,d.scheduled_for,d.decided_at,"
                "e.id AS trigger_id,e.source,e.external_id,e.payload_json,"
                "e.observed_at FROM trigger_decisions d "
                "JOIN trigger_events e ON e.id=d.trigger_id "
                "WHERE e.source_id=? ORDER BY d.decided_at DESC,d.id DESC LIMIT ?",
                (schedule_id, limit)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            out.append(item)
        return out

    def remove_owner(self, owner: str) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM trigger_events WHERE owner=?", (owner,))
            return cur.rowcount
