"""Persistent memory — the orchestrator's read/write 'world model' substrate.

Unlike RAG (read-only retrieval over documents) this is state the agent
*maintains* across runs: facts it learned, decisions it made, notes to its
future self. Backed by SQLite with FTS5 full-text search (falls back to LIKE if
FTS5 isn't compiled in). Marked private — it's your accumulated knowledge and
should not auto-forward to cloud LLMs.

Pair with kg.* (entities + relations) when you want structure; use memory.* for
free-form notes and facts.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

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
    _ensure_schema(conn)
    return conn


def _has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
    ).fetchone()
    return row is not None


def _ensure_schema(conn: sqlite3.Connection) -> None:
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
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), t


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
                "INSERT INTO memory(ts, epoch, kind, tags, content, source) "
                "VALUES (?,?,?,?,?,?)",
                (ts, epoch, args.get("kind", "note"), args.get("tags", ""),
                 args["content"], args.get("source", "")),
            )
            conn.commit()
            return ToolResult(status="ok", result={"id": cur.lastrowid, "ts": ts})
        finally:
            conn.close()


class MemorySearch(Tool):
    name = "memory.search"
    description = ("Full-text search persistent memory. Returns matching entries, "
                  "most relevant first. Optionally filter by kind.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search terms (FTS5 syntax ok)."},
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            "kind": {"type": "string", "description": "Optional kind filter."},
        },
        "required": ["query"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        q = args["query"]
        limit = int(args.get("limit", 10))
        kind = args.get("kind")
        conn = _connect(ctx)
        try:
            rows = []
            if _has_fts(conn):
                sql = ("SELECT m.id, m.ts, m.kind, m.tags, m.content, m.source "
                       "FROM memory_fts f JOIN memory m ON m.id = f.rowid "
                       "WHERE memory_fts MATCH ? ")
                params = [q]
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
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "integer"}},
        "required": ["id"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _connect(ctx)
        try:
            row = conn.execute(
                "SELECT id, ts, kind, tags, content, source FROM memory WHERE id = ?",
                (int(args["id"]),),
            ).fetchone()
            if not row:
                return ToolResult(status="error", result=None,
                                  error=f"no memory with id {args['id']}")
            return ToolResult(status="ok", result=dict(row))
        finally:
            conn.close()


class MemoryList(Tool):
    name = "memory.list"
    description = "List recent memory entries, newest first. Optionally filter by kind."
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            "kind": {"type": "string"},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _connect(ctx)
        try:
            sql = "SELECT id, ts, kind, tags, content, source FROM memory "
            params = []
            if args.get("kind"):
                sql += "WHERE kind = ? "
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
            cur = conn.execute("DELETE FROM memory WHERE id = ?", (int(args["id"]),))
            conn.commit()
            if cur.rowcount == 0:
                return ToolResult(status="error", result=None,
                                  error=f"no memory with id {args['id']}")
            return ToolResult(status="ok", result={"deleted": int(args["id"])})
        finally:
            conn.close()
