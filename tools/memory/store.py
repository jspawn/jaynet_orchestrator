"""Persistent memory — the orchestrator's read/write 'world model' substrate.

Unlike RAG (read-only retrieval over documents) this is state the agent
*maintains* across runs: facts it learned, decisions it made, notes to its
future self. Backed by SQLite with FTS5 full-text search (falls back to LIKE if
FTS5 isn't compiled in). Marked private — it's your accumulated knowledge and
should not auto-forward to cloud LLMs.

Pair with kg.* (entities + relations) when you want structure; use memory.* for
free-form notes and facts.

Owner scoping (audit 2026-10-05 finding 3): every entry carries the writing
run's owner; web users see only their own entries, the ownerless CLI path
sees all, and the read tools' all_owners=true is the confirmation-gated
escape — see runtime/owner_scope.py for the exact policy.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from runtime.owner_scope import legacy_owner, owner_clause, scoped_owner
from runtime.tool_base import Tool, ToolContext, ToolResult


def _db_path(ctx: ToolContext) -> str:
    from runtime.paths import MEMORY_DB
    return (ctx.config.get("tools", {}).get("memory", {})
            .get("db_path", str(MEMORY_DB)))


def _connect(ctx: ToolContext) -> sqlite3.Connection:
    path = _db_path(ctx)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn, legacy_owner(ctx.config))
    return conn


def _has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
    ).fetchone()
    return row is not None


def _ensure_schema(conn: sqlite3.Connection, legacy: str = "") -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            epoch REAL NOT NULL,
            kind TEXT DEFAULT 'note',
            tags TEXT DEFAULT '',
            content TEXT NOT NULL,
            source TEXT DEFAULT ''
        )
    """)
    # Migration: DBs from before owner scoping (audit finding 3). The
    # one-time UPDATE fires only inside this branch, so ownerless rows
    # written LATER by the CLI path are never re-assigned.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memory)")}
    if "owner" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN owner TEXT DEFAULT ''")
        if legacy:
            conn.execute("UPDATE memory SET owner = ? WHERE owner = ''",
                         (legacy,))
    # Try to build an FTS5 mirror. If the build lacks FTS5, swallow and use LIKE.
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                content, tags, content='memory', content_rowid='id'
            )
        """)
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
                INSERT INTO memory_fts(rowid, content, tags)
                VALUES (new.id, new.content, new.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, content, tags)
                VALUES ('delete', old.id, old.content, old.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, content, tags)
                VALUES ('delete', old.id, old.content, old.tags);
                INSERT INTO memory_fts(rowid, content, tags)
                VALUES (new.id, new.content, new.tags);
            END;
        """)
    except sqlite3.OperationalError:
        pass
    conn.commit()


def _now() -> tuple[str, float]:
    t = time.time()
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), t


class MemoryAppend(Tool):
    name = "memory.append"
    description = ("Save a note/fact/decision to persistent memory for recall in "
                  "future runs. Use kind to categorise (note, fact, decision, todo, "
                  "config) and tags for retrieval.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The text to remember."},
            "kind": {"type": "string", "default": "note",
                     "description": "note | fact | decision | todo | config | ..."},
            "tags": {"type": "string", "default": "",
                     "description": "Space- or comma-separated tags."},
            "source": {"type": "string", "default": "",
                       "description": "Where this came from (job id, url, file...)."},
        },
        "required": ["content"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        ts, epoch = _now()
        conn = _connect(ctx)
        try:
            cur = conn.execute(
                "INSERT INTO memory(ts, epoch, kind, tags, content, source, owner) "
                "VALUES (?,?,?,?,?,?,?)",
                (ts, epoch, args.get("kind", "note"), args.get("tags", ""),
                 args["content"], args.get("source", ""), scoped_owner(ctx)),
            )
            conn.commit()
            return ToolResult(status="ok", result={"id": cur.lastrowid, "ts": ts})
        finally:
            conn.close()


class MemorySearch(Tool):
    name = "memory.search"
    description = ("Full-text search persistent memory. Returns matching entries, "
                  "most relevant first. Optionally filter by kind. Scoped to YOUR "
                  "entries; all_owners=true is an admin/debug escape hatch that "
                  "searches every user's memory.")
    private = True
    read_only = True

    def needs_confirmation(self, args: dict, context: ToolContext) -> bool:
        # all_owners lifts the owner filter — a cross-user read of another
        # account's notes (audit finding 3; mirrors trace.query's gate).
        return bool(args.get("all_owners"))

    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search terms (FTS5 syntax ok)."},
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            "kind": {"type": "string", "description": "Optional kind filter."},
            "all_owners": {"type": "boolean", "default": False,
                           "description": "Admin/debug escape hatch: search all "
                                          "users' entries instead of only your own."},
        },
        "required": ["query"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        q = args["query"]
        limit = int(args.get("limit", 10))
        kind = args.get("kind")
        all_owners = bool(args.get("all_owners"))
        conn = _connect(ctx)
        try:
            rows = []
            if _has_fts(conn):
                sql = ("SELECT m.id, m.ts, m.kind, m.tags, m.content, m.source "
                       "FROM memory_fts f JOIN memory m ON m.id = f.rowid "
                       "WHERE memory_fts MATCH ? ")
                params = [q]
                frag, fparams = owner_clause(ctx, all_owners, "m.owner")
                sql += frag + " "
                params += fparams
                if kind:
                    sql += "AND m.kind = ? "
                    params.append(kind)
                sql += "ORDER BY rank LIMIT ?"
                params.append(limit)
                try:
                    rows = conn.execute(sql, params).fetchall()
                except sqlite3.OperationalError:
                    rows = []  # malformed FTS query -> fall through to LIKE
            if not rows:
                sql = ("SELECT id, ts, kind, tags, content, source FROM memory "
                       "WHERE content LIKE ? ")
                params = [f"%{q}%"]
                frag, fparams = owner_clause(ctx, all_owners)
                sql += frag + " "
                params += fparams
                if kind:
                    sql += "AND kind = ? "
                    params.append(kind)
                sql += "ORDER BY epoch DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
            out = [dict(r) for r in rows]
            for r in out:
                if len(r["content"]) > 600:
                    r["content"] = r["content"][:600] + "…"
            return ToolResult(status="ok", result={"matches": out, "count": len(out)})
        finally:
            conn.close()


class MemoryGet(Tool):
    name = "memory.get"
    description = "Fetch a single memory entry by id (full content, untruncated)."
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "integer"}},
        "required": ["id"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _connect(ctx)
        try:
            frag, params = owner_clause(ctx)
            row = conn.execute(
                "SELECT id, ts, kind, tags, content, source FROM memory "
                f"WHERE id = ?{frag}",
                (int(args["id"]), *params),
            ).fetchone()
            if not row:
                return ToolResult(status="error", result=None,
                                  error=f"no memory with id {args['id']}")
            return ToolResult(status="ok", result=dict(row))
        finally:
            conn.close()


class MemoryList(Tool):
    name = "memory.list"
    description = ("List recent memory entries, newest first. Optionally filter by "
                  "kind. Scoped to YOUR entries; all_owners=true is an admin/debug "
                  "escape hatch that lists every user's memory.")
    private = True
    read_only = True

    def needs_confirmation(self, args: dict, context: ToolContext) -> bool:
        # all_owners lifts the owner filter (audit finding 3).
        return bool(args.get("all_owners"))

    parameters = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            "kind": {"type": "string"},
            "all_owners": {"type": "boolean", "default": False,
                           "description": "Admin/debug escape hatch: list all "
                                          "users' entries instead of only your own."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _connect(ctx)
        try:
            all_owners = bool(args.get("all_owners"))
            frag, params = owner_clause(ctx, all_owners)
            sql = "SELECT id, ts, kind, tags, content, source FROM memory "
            sql += f"WHERE 1=1{frag} "
            if args.get("kind"):
                sql += "AND kind = ? "
                params.append(args["kind"])
            sql += "ORDER BY epoch DESC LIMIT ?"
            params.append(int(args.get("limit", 20)))
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            for r in rows:
                if len(r["content"]) > 300:
                    r["content"] = r["content"][:300] + "…"
            return ToolResult(status="ok", result={"entries": rows, "count": len(rows)})
        finally:
            conn.close()


class MemoryDelete(Tool):
    name = "memory.delete"
    description = "Delete a memory entry by id."
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "integer"}},
        "required": ["id"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _connect(ctx)
        try:
            frag, params = owner_clause(ctx)
            cur = conn.execute(
                f"DELETE FROM memory WHERE id = ?{frag}",
                (int(args["id"]), *params))
            conn.commit()
            if cur.rowcount == 0:
                return ToolResult(status="error", result=None,
                                  error=f"no memory with id {args['id']}")
            return ToolResult(status="ok", result={"deleted": int(args["id"])})
        finally:
            conn.close()
