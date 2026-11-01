"""Durable scheduled-prompt work, execution attempts, and chat outbox.

Shares chats.db with ChatStore and ScheduleRunStore. Agent execution itself is
not exactly-once: a launched attempt is interrupted on restart, never replayed
automatically. Scheduled-chat delivery is idempotent and safe to retry.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from web.store import ScheduleRunStore, ensure_private_store

_KEEP_PER_SCHEDULE = 50
_ACTIVE = ("queued", "running", "delivering")
_TERMINAL = ("completed", "failed", "cancelled")


def _places(values: tuple[str, ...]) -> str:
    return ",".join("?" * len(values))


class WorkStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        ScheduleRunStore(db_path)       # completion writes history in our transaction
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS work_items(
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    owner TEXT,
                    project_id TEXT,
                    source_id TEXT NOT NULL,
                    occurrence_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_work_source
                    ON work_items(source_id, status);
                CREATE INDEX IF NOT EXISTS idx_work_owner ON work_items(owner);
                CREATE TABLE IF NOT EXISTS work_attempts(
                    id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                    attempt_no INTEGER NOT NULL,
                    run_id TEXT UNIQUE,
                    status TEXT NOT NULL,
                    scheduled_for REAL NOT NULL,
                    claimed_at REAL,
                    launched_at REAL,
                    finished_at REAL,
                    duration_s REAL,
                    result_status TEXT,
                    excerpt TEXT,
                    tokens INTEGER,
                    error TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(item_id, attempt_no)
                );
                CREATE INDEX IF NOT EXISTS idx_attempt_queue
                    ON work_attempts(status, scheduled_for);
                CREATE TABLE IF NOT EXISTS work_effects(
                    id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                    attempt_id TEXT NOT NULL REFERENCES work_attempts(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    applied_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_effect_queue
                    ON work_effects(status, next_attempt_at);
            """)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_private_store(self.db_path)
        return conn

    @staticmethod
    def _item(row: sqlite3.Row) -> dict:
        out = dict(row)
        out["payload"] = json.loads(out.pop("payload_json"))
        return out

    @staticmethod
    def _prune(conn: sqlite3.Connection, source_id: str) -> None:
        conn.execute(
            "DELETE FROM schedule_runs WHERE schedule_id=? AND id NOT IN "
            "(SELECT id FROM schedule_runs WHERE schedule_id=? "
            "ORDER BY id DESC LIMIT ?)",
            (source_id, source_id, _KEEP_PER_SCHEDULE))
        old = conn.execute(
            f"SELECT id FROM work_items WHERE source_id=? "
            f"AND status IN ({_places(_TERMINAL)}) "
            "ORDER BY created_at DESC LIMIT -1 OFFSET ?",
            (source_id, *_TERMINAL, _KEEP_PER_SCHEDULE)).fetchall()
        if old:
            conn.execute(
                f"DELETE FROM work_items WHERE id IN ({_places(tuple(r['id'] for r in old))})",
                tuple(r["id"] for r in old))

    @staticmethod
    def _history(conn: sqlite3.Connection, item: sqlite3.Row,
                 attempt: sqlite3.Row | dict, now: float, status: str,
                 excerpt: str) -> None:
        conn.execute(
            "INSERT INTO schedule_runs(schedule_id,owner,run_id,fired_at,"
            "duration_s,status,excerpt,tokens) VALUES (?,?,?,?,?,?,?,?)",
            (item["source_id"], item["owner"], attempt["run_id"], now,
             attempt["duration_s"], status, excerpt[:500], attempt["tokens"]))

    def enqueue_occurrence(self, entry: dict,
                           scheduled_for: float | None = None) -> dict:
        """Insert item + first queued attempt atomically.

        outcome is enqueued, existing (same unique occurrence), or active
        (another unfinished occurrence for this schedule).
        """
        occurrence = float(scheduled_for if scheduled_for is not None
                           else entry["next_fire"])
        sid = str(entry["id"])
        key = f"schedule:{sid}:{occurrence}"
        now = time.time()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id,status FROM work_items WHERE occurrence_key=?",
                (key,)).fetchone()
            if existing:
                conn.commit()
                return {"outcome": "existing", "item_id": existing["id"],
                        "status": existing["status"]}
            active = conn.execute(
                f"SELECT id FROM work_items WHERE source_id=? "
                f"AND status IN ({_places(_ACTIVE)}) LIMIT 1",
                (sid, *_ACTIVE)).fetchone()
            if active:
                conn.commit()
                return {"outcome": "active", "item_id": active["id"]}
            item_id, attempt_id = uuid.uuid4().hex, uuid.uuid4().hex
            payload = dict(entry)
            conn.execute(
                "INSERT INTO work_items(id,kind,owner,project_id,source_id,"
                "occurrence_key,payload_json,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item_id, "scheduled_prompt", entry.get("owner"),
                 entry.get("project_id"), sid, key, json.dumps(payload),
                 "queued", now, now))
            conn.execute(
                "INSERT INTO work_attempts(id,item_id,attempt_no,run_id,status,"
                "scheduled_for,created_at) VALUES (?,?,?,?,?,?,?)",
                (attempt_id, item_id, 1, uuid.uuid4().hex, "queued",
                 occurrence, now))
            conn.commit()
            return {"outcome": "enqueued", "item_id": item_id,
                    "attempt_id": attempt_id}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_next(self) -> dict | None:
        """Atomically claim the oldest queued attempt and its item."""
        now = time.time()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT a.* FROM work_attempts a "
                "JOIN work_items i ON i.id=a.item_id "
                "WHERE a.status='queued' AND i.status='queued' "
                "ORDER BY a.scheduled_for,a.created_at LIMIT 1").fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE work_attempts SET status='running',claimed_at=? WHERE id=?",
                (now, row["id"]))
            conn.execute(
                "UPDATE work_items SET status='running',updated_at=? WHERE id=?",
                (now, row["item_id"]))
            item = conn.execute("SELECT * FROM work_items WHERE id=?",
                                (row["item_id"],)).fetchone()
            conn.commit()
            attempt = dict(row)
            attempt.update({"status": "running", "claimed_at": now})
            return {"item": self._item(item), "attempt": attempt}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_launched(self, attempt_id: str) -> bool:
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE work_attempts SET launched_at=? "
                "WHERE id=? AND status='running' AND launched_at IS NULL",
                (now, attempt_id))
            return cur.rowcount == 1

    def finish_attempt(self, attempt_id: str, *, result_status: str,
                       answer: str, duration_s: float | None,
                       tokens: int | None, excerpt: str | None = None,
                       error: str | None = None, deliver: bool = True) -> bool:
        """Finish execution, write compatibility history, and enqueue delivery."""
        now = time.time()
        success = result_status not in ("error", "failed")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            attempt = conn.execute("SELECT * FROM work_attempts WHERE id=?",
                                   (attempt_id,)).fetchone()
            if attempt is None or attempt["status"] != "running":
                conn.rollback()
                return False
            item = conn.execute("SELECT * FROM work_items WHERE id=?",
                                (attempt["item_id"],)).fetchone()
            if item is None:
                conn.rollback()
                return False
            text = excerpt if excerpt is not None else (error or answer)
            conn.execute(
                "UPDATE work_attempts SET status=?,finished_at=?,duration_s=?,"
                "result_status=?,excerpt=?,tokens=?,error=? WHERE id=?",
                ("succeeded" if success else "failed", now, duration_s,
                 result_status, text[:500], tokens, error, attempt_id))
            final = "completed" if success else "failed"
            conn.execute("UPDATE work_items SET status=?,updated_at=? WHERE id=?",
                         ("delivering" if deliver else final, now, item["id"]))
            stored = dict(attempt)
            stored.update({"duration_s": duration_s, "tokens": tokens})
            self._history(conn, item, stored, now, result_status, text)
            if deliver:
                payload = {"owner": item["owner"],
                           "prompt": json.loads(item["payload_json"]).get("prompt", ""),
                           "answer": answer, "run_id": attempt["run_id"],
                           "status": result_status}
                conn.execute(
                    "INSERT INTO work_effects(id,item_id,attempt_id,kind,dedupe_key,"
                    "payload_json,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, item["id"], attempt_id,
                     "scheduled_chat_append", f"scheduled-chat:{attempt['run_id']}",
                     json.dumps(payload), "pending", now))
            self._prune(conn, item["source_id"])
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def next_effect(self, now: float | None = None) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM work_effects WHERE status='pending' "
                "AND next_attempt_at<=? ORDER BY created_at LIMIT 1",
                (time.time() if now is None else now,)).fetchone()
            if row is None:
                return None
            out = dict(row)
            out["payload"] = json.loads(out.pop("payload_json"))
            return out

    def mark_effect_applied(self, effect_id: str) -> bool:
        now = time.time()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            effect = conn.execute("SELECT * FROM work_effects WHERE id=?",
                                  (effect_id,)).fetchone()
            if effect is None or effect["status"] == "applied":
                conn.rollback()
                return False
            conn.execute(
                "UPDATE work_effects SET status='applied',applied_at=?,"
                "last_error=NULL WHERE id=?", (now, effect_id))
            attempt = conn.execute("SELECT status FROM work_attempts WHERE id=?",
                                   (effect["attempt_id"],)).fetchone()
            pending = conn.execute(
                "SELECT 1 FROM work_effects WHERE item_id=? AND status!='applied'",
                (effect["item_id"],)).fetchone()
            if pending is None:
                final = "completed" if attempt and attempt["status"] == "succeeded" else "failed"
                conn.execute(
                    "UPDATE work_items SET status=?,updated_at=? "
                    "WHERE id=? AND status='delivering'",
                    (final, now, effect["item_id"]))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_effect_failed(self, effect_id: str, error: str,
                           now: float | None = None) -> None:
        ts = time.time() if now is None else now
        with self._conn() as conn:
            row = conn.execute("SELECT attempt_count FROM work_effects WHERE id=?",
                               (effect_id,)).fetchone()
            if row is None:
                return
            count = int(row["attempt_count"]) + 1
            delay = min(300, 5 * (2 ** min(count - 1, 6)))
            conn.execute(
                "UPDATE work_effects SET attempt_count=?,next_attempt_at=?,"
                "last_error=? WHERE id=? AND status='pending'",
                (count, ts + delay, error[:500], effect_id))

    def _cancel(self, where: str, args: tuple, reason: str) -> int:
        now = time.time()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT i.*,a.id AS attempt_id,a.run_id,a.duration_s,a.tokens "
                "FROM work_items i JOIN work_attempts a ON a.item_id=i.id "
                f"WHERE i.status='queued' AND a.status='queued' AND {where}",
                args).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE work_attempts SET status='cancelled',finished_at=?,"
                    "result_status='cancelled',excerpt=?,error=? WHERE id=?",
                    (now, reason[:500], reason, row["attempt_id"]))
                conn.execute("UPDATE work_items SET status='cancelled',updated_at=? "
                             "WHERE id=?", (now, row["id"]))
                self._history(conn, row, row, now, "cancelled", reason)
                self._prune(conn, row["source_id"])
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def cancel_source(self, source_id: str, reason: str = "schedule disabled") -> int:
        return self._cancel("i.source_id=?", (source_id,), reason)

    def cancel_occurrence(self, occurrence_key: str,
                          reason: str = "schedule changed before launch") -> int:
        return self._cancel("i.occurrence_key=?", (occurrence_key,), reason)

    def recover(self) -> dict:
        """Reconcile process crashes without replaying launched agent work."""
        now = time.time()
        counts = {"requeued": 0, "interrupted": 0, "completed_delivery": 0}
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            running = conn.execute(
                "SELECT a.*,i.source_id,i.owner FROM work_attempts a "
                "JOIN work_items i ON i.id=a.item_id WHERE a.status='running'").fetchall()
            for attempt in running:
                if attempt["launched_at"] is None:
                    conn.execute(
                        "UPDATE work_attempts SET status='queued',claimed_at=NULL "
                        "WHERE id=?", (attempt["id"],))
                    conn.execute("UPDATE work_items SET status='queued',updated_at=? "
                                 "WHERE id=?", (now, attempt["item_id"]))
                    counts["requeued"] += 1
                    continue
                error = ("interrupted: service restarted after launch; "
                         "inspect workspace and external state before retry")
                duration = max(0.0, now - attempt["launched_at"])
                conn.execute(
                    "UPDATE work_attempts SET status='interrupted',finished_at=?,"
                    "duration_s=?,result_status='interrupted',excerpt=?,error=? "
                    "WHERE id=?",
                    (now, duration, error, error, attempt["id"]))
                conn.execute("UPDATE work_items SET status='interrupted',updated_at=? "
                             "WHERE id=?", (now, attempt["item_id"]))
                stored = dict(attempt)
                stored.update({"duration_s": duration, "tokens": None})
                item = conn.execute("SELECT * FROM work_items WHERE id=?",
                                    (attempt["item_id"],)).fetchone()
                self._history(conn, item, stored, now, "interrupted", error)
                self._prune(conn, attempt["source_id"])
                counts["interrupted"] += 1
            delivered = conn.execute(
                "SELECT DISTINCT i.id,a.status AS attempt_status FROM work_items i "
                "JOIN work_attempts a ON a.item_id=i.id "
                "WHERE i.status='delivering' AND NOT EXISTS "
                "(SELECT 1 FROM work_effects e WHERE e.item_id=i.id "
                "AND e.status!='applied')").fetchall()
            for row in delivered:
                final = "completed" if row["attempt_status"] == "succeeded" else "failed"
                conn.execute("UPDATE work_items SET status=?,updated_at=? WHERE id=?",
                             (final, now, row["id"]))
                counts["completed_delivery"] += 1
            conn.commit()
            return counts
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def history(self, schedule_id: str, owner: str | None = None,
                limit: int = 100) -> list[dict]:
        """Compatibility history enriched with work/attempt/delivery metadata."""
        where = ["sr.schedule_id=?"]
        args: list[object] = [schedule_id]
        if owner is not None:
            where.append("sr.owner=?")
            args.append(owner)
        args.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT sr.*,wi.id AS item_id,wi.status AS work_status,"
                "wa.id AS attempt_id,wa.attempt_no,wa.scheduled_for,"
                "wa.status AS attempt_status,wa.error AS work_error,"
                "we.status AS delivery_status,we.attempt_count AS delivery_attempts,"
                "we.last_error AS delivery_error "
                "FROM schedule_runs sr "
                "LEFT JOIN work_attempts wa ON wa.run_id=sr.run_id "
                "LEFT JOIN work_items wi ON wi.id=wa.item_id "
                "LEFT JOIN work_effects we ON we.attempt_id=wa.id "
                f"WHERE {' AND '.join(where)} ORDER BY sr.id DESC LIMIT ?",
                tuple(args)).fetchall()
            return [dict(r) for r in rows]

    def remove_owner(self, owner: str) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM work_items WHERE owner=?", (owner,))
            conn.execute("DELETE FROM schedule_runs WHERE owner=?", (owner,))
            return cur.rowcount
