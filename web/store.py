"""Saved-chat store for the web console.

Deliberately simple: a chat exists in the `chat` table *only* if the user
explicitly saved it. "Mark to be saved" -> upsert; "remove to be saved" ->
delete. There is no `saved` flag because presence is the flag.

The one exception is `current_chat`: exactly one row per owner holding the
*active* (possibly unsaved) chat snapshot, so the same session follows the
user across browsers/devices. It is not a saved chat — it never shows in the
list and is replaced on every change (last writer wins).

Same shape as the other /srv/orchestrator SQLite stores (trace/memory/kg/rag).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ChatStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS chat(
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_turn(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    user_message TEXT NOT NULL,
                    answer TEXT DEFAULT '',
                    run_id TEXT,
                    status TEXT,
                    events TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_turn_chat ON chat_turn(chat_id);
                CREATE TABLE IF NOT EXISTS current_chat(
                    owner TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    active_run TEXT,
                    updated_at TEXT NOT NULL
                );
            """)
            # Migration: per-user ownership. Legacy rows keep owner NULL.
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(chat)")]
            if "owner" not in cols:
                conn.execute("ALTER TABLE chat ADD COLUMN owner TEXT")
            if "project_id" not in cols:
                conn.execute("ALTER TABLE chat ADD COLUMN project_id TEXT")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def list(self, owner: str | None = None) -> list[dict]:
        with self._conn() as conn:
            where = "WHERE c.owner=?" if owner is not None else ""
            args = (owner,) if owner is not None else ()
            rows = conn.execute(
                "SELECT c.id, c.title, c.created_at, c.updated_at, c.project_id, "
                "  (SELECT COUNT(*) FROM chat_turn t WHERE t.chat_id=c.id) AS turns "
                f"FROM chat c {where} ORDER BY c.updated_at DESC", args).fetchall()
            return [dict(r) for r in rows]

    def get(self, chat_id: str, owner: str | None = None) -> dict | None:
        with self._conn() as conn:
            c = conn.execute("SELECT * FROM chat WHERE id=?", (chat_id,)).fetchone()
            if not c:
                return None
            if owner is not None and c["owner"] is not None and c["owner"] != owner:
                return None  # not yours
            turns = conn.execute(
                "SELECT idx, user_message, answer, run_id, status, events "
                "FROM chat_turn WHERE chat_id=? ORDER BY idx", (chat_id,)).fetchall()
            out = dict(c)
            out["turns"] = []
            for t in turns:
                d = dict(t)
                try:
                    d["events"] = json.loads(d["events"] or "[]")
                except Exception:
                    d["events"] = []
                out["turns"].append(d)
            return out

    def upsert(self, chat_id: str | None, title: str | None,
               turns: list[dict], owner: str | None = None,
               project_id: str | None = None) -> dict | None:
        """Create or replace a saved chat. Turns are fully replaced each save so a
        saved chat stays in sync as the conversation grows. `owner` is set on
        create and preserved on update. Returns None without touching anything
        when the id already belongs to a different owner. A legacy owner-NULL
        row is claimed by the first upsert that carries an owner (read stays
        shared until then; rename/delete are admin-only — see below)."""
        cid = chat_id or uuid.uuid4().hex
        now = _now()
        if not title:
            first = turns[0]["user_message"] if turns else "(empty)"
            title = (first[:57] + "…") if len(first) > 60 else first
        with self._conn() as conn:
            exists = conn.execute("SELECT created_at, owner FROM chat WHERE id=?",
                                  (cid,)).fetchone()
            if (exists and owner is not None and exists["owner"] is not None
                    and exists["owner"] != owner):
                return None  # not yours
            created = exists["created_at"] if exists else now
            owner_val = (exists["owner"] or owner) if exists else owner  # claims NULL rows
            conn.execute(
                "INSERT INTO chat(id,title,created_at,updated_at,owner,project_id) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, "
                "updated_at=excluded.updated_at, owner=excluded.owner, "
                "project_id=excluded.project_id",
                (cid, title, created, now, owner_val, project_id))
            conn.execute("DELETE FROM chat_turn WHERE chat_id=?", (cid,))
            for i, t in enumerate(turns):
                conn.execute(
                    "INSERT INTO chat_turn(chat_id,idx,user_message,answer,run_id,"
                    "status,events,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (cid, i, t.get("user_message", ""), t.get("answer", ""),
                     t.get("run_id"), t.get("status"),
                     json.dumps(t.get("events") or []), now))
        return {"id": cid, "title": title, "created_at": created, "updated_at": now,
                "turns": len(turns)}

    def rename(self, chat_id: str, title: str, owner: str | None = None,
               is_admin: bool = False) -> bool:
        # Legacy owner-NULL rows are shared read-only history: only their owner
        # can rename — and they have none, so rename is admin-only until an
        # upsert claims the row.
        with self._conn() as conn:
            if owner is not None and not is_admin:
                cur = conn.execute(
                    "UPDATE chat SET title=?, updated_at=? WHERE id=? AND owner=?",
                    (title, _now(), chat_id, owner))
            else:
                cur = conn.execute("UPDATE chat SET title=?, updated_at=? WHERE id=?",
                                   (title, _now(), chat_id))
            return cur.rowcount > 0

    def delete(self, chat_id: str, owner: str | None = None,
               is_admin: bool = False) -> bool:
        with self._conn() as conn:
            if owner is not None and not is_admin:
                # Same rule as rename: owner-NULL legacy rows are admin-only.
                owned = conn.execute(
                    "SELECT 1 FROM chat WHERE id=? AND owner=?",
                    (chat_id, owner)).fetchone()
                if not owned:
                    return False
            conn.execute("DELETE FROM chat_turn WHERE chat_id=?", (chat_id,))
            cur = conn.execute("DELETE FROM chat WHERE id=?", (chat_id,))
            return cur.rowcount > 0

    def delete_owner(self, owner: str) -> int:
        """Delete every chat (and its turns) owned by `owner` — user deletion."""
        with self._conn() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM chat WHERE owner=?", (owner,)).fetchall()]
            for cid in ids:
                conn.execute("DELETE FROM chat_turn WHERE chat_id=?", (cid,))
            conn.execute("DELETE FROM current_chat WHERE owner=?", (owner,))
            cur = conn.execute("DELETE FROM chat WHERE owner=?", (owner,))
            return cur.rowcount

    # ---- current (unsaved) chat: one row per owner, last writer wins --------
    def get_current(self, owner: str) -> dict | None:
        """The owner's active-chat snapshot, or None if never synced/cleared."""
        with self._conn() as conn:
            r = conn.execute(
                "SELECT payload, active_run, updated_at FROM current_chat "
                "WHERE owner=?", (owner,)).fetchone()
            if not r:
                return None
            try:
                chat = json.loads(r["payload"])
            except Exception:
                return None
            return {"chat": chat, "active_run": r["active_run"],
                    "updated_at": r["updated_at"]}

    def set_current(self, owner: str, payload: dict,
                    active_run: str | None = None) -> dict:
        """Replace the owner's active-chat snapshot (full replace, no merge)."""
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO current_chat(owner,payload,active_run,updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(owner) DO UPDATE SET "
                "payload=excluded.payload, active_run=excluded.active_run, "
                "updated_at=excluded.updated_at",
                (owner, json.dumps(payload), active_run, now))
        return {"ok": True, "updated_at": now}

    def clear_current(self, owner: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM current_chat WHERE owner=?", (owner,))
            return cur.rowcount > 0
