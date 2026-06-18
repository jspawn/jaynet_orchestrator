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
    user_message TEXT,
    final_answer TEXT,
    status TEXT,           -- "ok" | "error" | "budget_exceeded"
    error TEXT,
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
    def __init__(self, db_path: str | Path, log_content: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_content = log_content
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.executescript(SCHEMA)

    def start_run(self, run_id: str, user_message: str) -> None:
        self._conn.execute(
            "INSERT INTO runs (id, started_at, user_message, status) VALUES (?, ?, ?, ?)",
            (run_id, time.time(), user_message if self.log_content else "", "running"),
        )

    def finish_run(self, run_id: str, status: str, final_answer: str = "",
                   error: str = "", summary: dict | None = None) -> None:
        self._conn.execute(
            """UPDATE runs SET finished_at=?, status=?, final_answer=?, error=?, summary_json=?
               WHERE id=?""",
            (time.time(), status,
             final_answer if self.log_content else "",
             error,
             json.dumps(summary) if summary else None,
             run_id),
        )

    def log(self, run_id: str, kind: str, iteration: int, payload: Any) -> int:
        """Record an event. Returns the event id."""
        if not self.log_content and kind in ("tool_call", "tool_result"):
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
        if isinstance(payload, dict):
            return {k: ("<stripped>" if k in ("result", "content", "args") else v)
                    for k, v in payload.items()}
        return payload

    def close(self) -> None:
        self._conn.close()
