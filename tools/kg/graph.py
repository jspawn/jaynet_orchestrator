"""Knowledge graph — structured half of the local 'world model'.

Entities (typed, with JSON attrs) and directed relations between them. Shares
the same SQLite file as memory.* but its own tables. Use this when structure
matters — 'model X was trained on dataset Y', 'job Z produced checkpoint C',
'R9700 has 32GB VRAM' — and you want to traverse relationships rather than
search free text.

Marked private. Entities/relations are auto-created on first reference so the
agent can build the graph incrementally.

Owner scoping (audit 2026-10-05 finding 3): entities and relations carry the
writing run's owner and uniqueness is per (owner, …) — two accounts can hold
same-named entities without sharing them. Web users see only their own
graph, the ownerless CLI path sees all, and the read tools' all_owners=true
is the confirmation-gated escape — see runtime/owner_scope.py.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from runtime.owner_scope import legacy_owner, owner_clause, scoped_owner
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
    _ensure_schema(conn, legacy_owner(ctx.config))
    return conn


_SCHEMA = """
    CREATE TABLE IF NOT EXISTS kg_entity(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        type TEXT DEFAULT '',
        attrs TEXT DEFAULT '{}',
        ts TEXT NOT NULL,
        UNIQUE(owner, name)
    );
    CREATE TABLE IF NOT EXISTS kg_relation(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT NOT NULL DEFAULT '',
        src TEXT NOT NULL,
        rel TEXT NOT NULL,
        dst TEXT NOT NULL,
        attrs TEXT DEFAULT '{}',
        ts TEXT NOT NULL,
        UNIQUE(owner, src, rel, dst)
    );
    CREATE INDEX IF NOT EXISTS idx_rel_src ON kg_relation(owner, src);
    CREATE INDEX IF NOT EXISTS idx_rel_dst ON kg_relation(owner, dst);
"""


def _ensure_schema(conn: sqlite3.Connection, legacy: str = "") -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_entity'"
    ).fetchone()
    if row is None:
        conn.executescript(_SCHEMA)
        conn.commit()
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(kg_entity)")}
    if "owner" in cols:
        return
    # Migration: DBs from before owner scoping (audit finding 3) — the old
    # GLOBAL name/(src,rel,dst) uniques can't be altered in place, so the
    # tables are rebuilt with per-owner composite uniques and the legacy
    # rows assigned to the box's first admin (runtime/owner_scope.py).
    lit = legacy.replace("'", "''")
    conn.executescript(f"""
        CREATE TABLE kg_entity_new(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            type TEXT DEFAULT '',
            attrs TEXT DEFAULT '{{}}',
            ts TEXT NOT NULL,
            UNIQUE(owner, name)
        );
        INSERT INTO kg_entity_new(id, owner, name, type, attrs, ts)
            SELECT id, '{lit}', name, type, attrs, ts FROM kg_entity;
        DROP TABLE kg_entity;
        ALTER TABLE kg_entity_new RENAME TO kg_entity;
        CREATE TABLE kg_relation_new(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL DEFAULT '',
            src TEXT NOT NULL,
            rel TEXT NOT NULL,
            dst TEXT NOT NULL,
            attrs TEXT DEFAULT '{{}}',
            ts TEXT NOT NULL,
            UNIQUE(owner, src, rel, dst)
        );
        INSERT INTO kg_relation_new(id, owner, src, rel, dst, attrs, ts)
            SELECT id, '{lit}', src, rel, dst, attrs, ts FROM kg_relation;
        DROP TABLE kg_relation;
        ALTER TABLE kg_relation_new RENAME TO kg_relation;
        CREATE INDEX IF NOT EXISTS idx_rel_src ON kg_relation(owner, src);
        CREATE INDEX IF NOT EXISTS idx_rel_dst ON kg_relation(owner, dst);
    """)
    conn.commit()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(ctx: ToolContext, entities: list[dict], relations: list[dict]) -> dict:
    """Bulk-upsert entities + relations over ONE connection/commit — the
    public entry point for auto-derived seeding (the graphify plugin's
    graph.seed_kg builds the curated graph from a project graph). Same merge
    semantics as the interactive tools: entities merge attrs by unique name,
    relations upsert by (src, rel, dst), missing endpoints are auto-created.
    entities: {name, type?, attrs?}; relations: {src, rel, dst, attrs?}.
    Returns {"entities": n, "relations": n} (attempted, not changed)."""
    owner = scoped_owner(ctx)
    conn = _connect(ctx)
    try:
        for e in entities:
            _upsert_entity(conn, str(e["name"]), str(e.get("type") or ""),
                           e.get("attrs"), owner)
        for r in relations:
            src, dst = str(r["src"]), str(r["dst"])
            _upsert_entity(conn, src, owner=owner)
            _upsert_entity(conn, dst, owner=owner)
            conn.execute(
                "INSERT INTO kg_relation(owner, src, rel, dst, attrs, ts) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(owner, src, rel, dst) DO UPDATE SET "
                "attrs=excluded.attrs, ts=excluded.ts",
                (owner, src, str(r["rel"]), dst,
                 json.dumps(r.get("attrs") or {}), _now()),
            )
        conn.commit()
        return {"entities": len(entities), "relations": len(relations)}
    finally:
        conn.close()


def _upsert_entity(conn: sqlite3.Connection, name: str, etype: str = "",
                   attrs: dict | None = None, owner: str = "") -> None:
    row = conn.execute(
        "SELECT attrs, type FROM kg_entity WHERE owner=? AND name=?",
        (owner, name)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO kg_entity(owner, name, type, attrs, ts) VALUES (?,?,?,?,?)",
            (owner, name, etype or "", json.dumps(attrs or {}), _now()),
        )
    else:
        merged = {}
        try:
            merged = json.loads(row["attrs"] or "{}")
        except Exception:
            merged = {}
        merged.update(attrs or {})
        conn.execute(
            "UPDATE kg_entity SET type=?, attrs=?, ts=? WHERE owner=? AND name=?",
            (etype or row["type"] or "", json.dumps(merged), _now(), owner, name),
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
        owner = scoped_owner(ctx)
        conn = _connect(ctx)
        try:
            _upsert_entity(conn, args["name"], args.get("type", ""),
                           args.get("attrs"), owner)
            conn.commit()
            row = conn.execute(
                "SELECT name, type, attrs, ts FROM kg_entity "
                "WHERE owner=? AND name=?",
                (owner, args["name"])).fetchone()
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
        owner = scoped_owner(ctx)
        conn = _connect(ctx)
        try:
            _upsert_entity(conn, args["src"], owner=owner)
            _upsert_entity(conn, args["dst"], owner=owner)
            conn.execute(
                "INSERT INTO kg_relation(owner, src, rel, dst, attrs, ts) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(owner, src, rel, dst) DO UPDATE SET "
                "attrs=excluded.attrs, ts=excluded.ts",
                (owner, args["src"], args["rel"], args["dst"],
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
                  "Returns entities with their attributes. Scoped to YOUR "
                  "entities; all_owners=true is an admin/debug escape hatch "
                  "that reads every user's graph.")
    private = True
    read_only = True

    def needs_confirmation(self, args: dict, context: ToolContext) -> bool:
        # all_owners lifts the owner filter (audit finding 3).
        return bool(args.get("all_owners"))

    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name or substring to match."},
            "type": {"type": "string", "description": "Filter by entity type."},
            "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 200},
            "all_owners": {"type": "boolean", "default": False,
                           "description": "Admin/debug escape hatch: read all "
                                          "users' entities instead of only your own."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _connect(ctx)
        try:
            all_owners = bool(args.get("all_owners"))
            frag, params = owner_clause(ctx, all_owners)
            sql = f"SELECT name, type, attrs, ts FROM kg_entity WHERE 1=1{frag} "
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
                  "relations up to `depth` hops. Use to traverse how things connect. "
                  "Scoped to YOUR relations; all_owners=true is an admin/debug "
                  "escape hatch that traverses every user's graph.")
    private = True
    read_only = True

    def needs_confirmation(self, args: dict, context: ToolContext) -> bool:
        # all_owners lifts the owner filter (audit finding 3).
        return bool(args.get("all_owners"))

    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Entity to expand from."},
            "depth": {"type": "integer", "default": 1, "minimum": 1, "maximum": 3},
            "all_owners": {"type": "boolean", "default": False,
                           "description": "Admin/debug escape hatch: traverse all "
                                          "users' relations instead of only your own."},
        },
        "required": ["name"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _connect(ctx)
        try:
            all_owners = bool(args.get("all_owners"))
            frag, fparams = owner_clause(ctx, all_owners)
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
                    f"WHERE (src IN ({placeholders}) OR dst IN ({placeholders}))"
                    f"{frag}",
                    list(frontier) + list(frontier) + fparams,
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
            frag, params = owner_clause(ctx)
            cur = conn.execute(
                f"DELETE FROM kg_relation WHERE src=? AND rel=? AND dst=?{frag}",
                (args["src"], args["rel"], args["dst"], *params),
            )
            conn.commit()
            if cur.rowcount == 0:
                return ToolResult(status="error", result=None, error="no such relation")
            return ToolResult(status="ok", result={
                "removed": f"{args['src']} -[{args['rel']}]-> {args['dst']}"})
        finally:
            conn.close()
