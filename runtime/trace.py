"""Reasoning trace logger.

Every step the agent takes (model turn, tool call, tool result, error) is
logged to SQLite. Useful for debugging, replay, and analyzing cost drivers.

Schema is intentionally simple: one `runs` table for request-level metadata,
one `events` table for the per-step log.
"""

from __future__ import annotations

import json
import time
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    finished_at REAL,
    owner TEXT,
    user_message TEXT,
    final_answer TEXT,
    status TEXT,           -- "ok" | "error" | "budget_exceeded"
    error TEXT,
    total_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,    -- "model_turn" | "tool_call" | "tool_result" | "error"
    iteration INTEGER,
    payload_json TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, ts);
"""


class Trace:
    def __init__(self, db_path: str | Path, log_content: bool = True,
                 retention_days: float = 0):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_content = log_content
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None,
                                     check_same_thread=False)
        # WAL + NORMAL sync: readers never block the writer and per-event
        # commits get much cheaper; durability is unchanged short of power loss.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        # Migrate older DBs that predate per-user usage tracking.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(runs)")}
        for name, decl in (("owner", "TEXT"),
                           ("total_tokens", "INTEGER DEFAULT 0"),
                           ("cost_usd", "REAL DEFAULT 0")):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")
        # Index for per-user usage aggregation (after the owner column exists).
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_owner ON runs(owner, started_at)")
        # Optional retention: prune runs (and their events) older than this.
        # 0 = keep everything. log_content=true makes the DB grow fast, so a
        # bound keeps the trace.query snappy over months of runs.
        if retention_days and retention_days > 0:
            cutoff = time.time() - float(retention_days) * 86400
            self._conn.execute(
                "DELETE FROM events WHERE run_id IN "
                "(SELECT id FROM runs WHERE started_at < ?)", (cutoff,))
            self._conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))

    def start_run(self, run_id: str, user_message: str, owner: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO runs (id, started_at, owner, user_message, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, time.time(), owner,
             user_message if self.log_content else "", "running"),
        )

    def finish_run(self, run_id: str, status: str, final_answer: str = "",
                   error: str = "", summary: dict | None = None) -> None:
        tokens = int(((summary or {}).get("tokens") or {}).get("total") or 0)
        cost = float((summary or {}).get("cost_usd") or 0.0)
        self._conn.execute(
            """UPDATE runs SET finished_at=?, status=?, final_answer=?, error=?,
               total_tokens=?, cost_usd=?, summary_json=? WHERE id=?""",
            (time.time(), status,
             final_answer if self.log_content else "",
             error, tokens, cost,
             json.dumps(summary) if summary else None,
             run_id),
        )

    def log(self, run_id: str, kind: str, iteration: int, payload: Any) -> int:
        """Record an event. Returns the event id."""
        if not self.log_content and kind in ("tool_call", "tool_result",
                                             "run_start", "run_finish", "verify"):
            # Strip large content fields, keep metadata
            payload = self._strip_content(payload)
        cur = self._conn.execute(
            "INSERT INTO events (run_id, ts, kind, iteration, payload_json) VALUES (?, ?, ?, ?, ?)",
            (run_id, time.time(), kind, iteration,
             json.dumps(payload, default=str, ensure_ascii=False)),
        )
        return cur.lastrowid

    @staticmethod
    def _strip_content(payload: Any) -> Any:
        # Content-bearing keys across event kinds: tool args/results, model
        # prose, the user's message, the final answer, and verifier output.
        if isinstance(payload, dict):
            return {k: ("<stripped>" if k in ("result", "content", "args",
                                              "message", "answer",
                                              "result_preview", "report") else v)
                    for k, v in payload.items()}
        return payload

    def close(self) -> None:
        self._conn.close()
