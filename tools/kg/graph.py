"""Knowledge graph — structured half of the local 'world model'.

Entities (typed, with JSON attrs) and directed relations between them. Shares
the same SQLite file as memory.* but its own tables. Use this when structure
matters — 'model X was trained on dataset Y', 'job Z produced checkpoint C',
'R9700 has 32GB VRAM' — and you want to traverse relationships rather than
search free text.

Marked private. Entities/relations are auto-created on first reference so the
agent can build the graph incrementally.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult


def _db_path(ctx: ToolContext) -> str:
    # Default to the same DB as memory.* unless kg.db_path overrides it.
    from runtime.paths import MEMORY_DB
    tools = ctx.config.get("tools", {})
    return (tools.get("kg", {}).get("db_path")
            or tools.get("memory", {}).get("db_path")
            or str(MEMORY_DB))


def _connect(ctx: ToolContext) -> sqlite3.Connection:
    path = _db_path(ctx)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kg_entity(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            type TEXT DEFAULT '',
            attrs TEXT DEFAULT '{}',
            ts TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kg_relation(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src TEXT NOT NULL,
            rel TEXT NOT NULL,
            dst TEXT NOT NULL,
            attrs TEXT DEFAULT '{}',
            ts TEXT NOT NULL,
            UNIQUE(src, rel, dst)
        );
        CREATE INDEX IF NOT EXISTS idx_rel_src ON kg_relation(src);
        CREATE INDEX IF NOT EXISTS idx_rel_dst ON kg_relation(dst);
    """)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _upsert_entity(conn: sqlite3.Connection, name: str, etype: str = "",
                   attrs: dict | None = None) -> None:
    row = conn.execute("SELECT attrs, type FROM kg_entity WHERE name=?", (name,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO kg_entity(name, type, attrs, ts) VALUES (?,?,?,?)",
            (name, etype or "", json.dumps(attrs or {}), _now()),
        )
    else:
        merged = {}
        try:
            merged = json.loads(row["attrs"] or "{}")
        except Exception:
            merged = {}
        merged.update(attrs or {})
        conn.execute(
            "UPDATE kg_entity SET type=?, attrs=?, ts=? WHERE name=?",
            (etype or row["type"] or "", json.dumps(merged), _now(), name),
        )


class KgUpsertEntity(Tool):
    name = "kg.upsert_entity"
    description = ("Create or update a typed entity with JSON attributes. attrs are "
                  "merged into any existing attrs. Use for things you want to track: "
                  "models, datasets, GPUs, jobs, checkpoints, papers.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Unique entity name/id."},
            "type": {"type": "string", "description": "Entity type, e.g. 'model', 'dataset'."},
            "attrs": {"type": "object", "description": "Arbitrary JSON attributes."},
        },
        "required": ["name"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _connect(ctx)
        try:
            _upsert_entity(conn, args["name"], args.get("type", ""), args.get("attrs"))
            conn.commit()
            row = conn.execute(
                "SELECT name, type, attrs, ts FROM kg_entity WHERE name=?",
                (args["name"],)).fetchone()
            d = dict(row)
            d["attrs"] = json.loads(d["attrs"] or "{}")
            return ToolResult(status="ok", result=d)
        finally:
            conn.close()


class KgAddRelation(Tool):
    name = "kg.add_relation"
    description = ("Add a directed relation src -[rel]-> dst. Both entities are "
                  "auto-created if missing. Example: src='qwen3-35b', "
                  "rel='quantized_as', dst='qwen3-35b-Q4_K_L'.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "src": {"type": "string"},
            "rel": {"type": "string", "description": "Relation/predicate."},
            "dst": {"type": "string"},
            "attrs": {"type": "object", "description": "Optional JSON attributes on the edge."},
        },
        "required": ["src", "rel", "dst"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _connect(ctx)
        try:
            _upsert_entity(conn, args["src"])
            _upsert_entity(conn, args["dst"])
            conn.execute(
                "INSERT INTO kg_relation(src, rel, dst, attrs, ts) VALUES (?,?,?,?,?) "
                "ON CONFLICT(src, rel, dst) DO UPDATE SET attrs=excluded.attrs, ts=excluded.ts",
                (args["src"], args["rel"], args["dst"],
                 json.dumps(args.get("attrs") or {}), _now()),
            )
            conn.commit()
            return ToolResult(status="ok", result={
                "edge": f"{args['src']} -[{args['rel']}]-> {args['dst']}"})
        finally:
            conn.close()


class KgQuery(Tool):
    name = "kg.query"
    description = ("Look up entities by name (exact or substring) and/or type. "
                  "Returns entities with their attributes.")
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name or substring to match."},
            "type": {"type": "string", "description": "Filter by entity type."},
            "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 200},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _connect(ctx)
        try:
            sql = "SELECT name, type, attrs, ts FROM kg_entity WHERE 1=1 "
            params = []
            if args.get("name"):
                sql += "AND name LIKE ? "
                params.append(f"%{args['name']}%")
            if args.get("type"):
                sql += "AND type = ? "
                params.append(args["type"])
            sql += "ORDER BY name LIMIT ?"
            params.append(int(args.get("limit", 25)))
            rows = []
            for r in conn.execute(sql, params).fetchall():
                d = dict(r)
                d["attrs"] = json.loads(d["attrs"] or "{}")
                rows.append(d)
            return ToolResult(status="ok", result={"entities": rows, "count": len(rows)})
        finally:
            conn.close()


class KgNeighbors(Tool):
    name = "kg.neighbors"
    description = ("Return the subgraph around an entity: outgoing and incoming "
                  "relations up to `depth` hops. Use to traverse how things connect.")
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Entity to expand from."},
            "depth": {"type": "integer", "default": 1, "minimum": 1, "maximum": 3},
        },
        "required": ["name"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _connect(ctx)
        try:
            start = args["name"]
            seen = {start}
            frontier = {start}
            edges = []
            for _ in range(int(args.get("depth", 1))):
                if not frontier:
                    break
                placeholders = ",".join("?" * len(frontier))
                rows = conn.execute(
                    f"SELECT src, rel, dst FROM kg_relation "
                    f"WHERE src IN ({placeholders}) OR dst IN ({placeholders})",
                    list(frontier) + list(frontier),
                ).fetchall()
                next_frontier = set()
                for r in rows:
                    edge = {"src": r["src"], "rel": r["rel"], "dst": r["dst"]}
                    if edge not in edges:
                        edges.append(edge)
                    for node in (r["src"], r["dst"]):
                        if node not in seen:
                            seen.add(node)
                            next_frontier.add(node)
                frontier = next_frontier
            return ToolResult(status="ok", result={
                "root": start,
                "nodes": sorted(seen),
                "edges": edges,
                "edge_count": len(edges),
            })
        finally:
            conn.close()


class KgRemoveRelation(Tool):
    name = "kg.remove_relation"
    description = "Remove a specific relation src -[rel]-> dst."
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "src": {"type": "string"},
            "rel": {"type": "string"},
            "dst": {"type": "string"},
        },
        "required": ["src", "rel", "dst"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _connect(ctx)
        try:
            cur = conn.execute(
                "DELETE FROM kg_relation WHERE src=? AND rel=? AND dst=?",
                (args["src"], args["rel"], args["dst"]),
            )
            conn.commit()
            if cur.rowcount == 0:
                return ToolResult(status="error", result=None, error="no such relation")
            return ToolResult(status="ok", result={
                "removed": f"{args['src']} -[{args['rel']}]-> {args['dst']}"})
        finally:
            conn.close()
