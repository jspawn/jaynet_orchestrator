"""Knowledge graph — structured half of the local 'world model'.

Entities (typed, with JSON attrs) and directed relations between them. Shares
the same SQLite file as memory.* but its own tables. Use this when structure
matters — 'model X was trained on dataset Y', 'job Z produced checkpoint C',
'R9700 has 32GB VRAM' — and you want to traverse relationships rather than
search free text.

Marked private. Entities/relations are auto-created on first reference so the
agent can build the graph incrementally. Aliases canonicalize alternate names;
explicit associations retain weight-sorted context recalled together.

Owner scoping (audit 2026-10-05 finding 3): entities, relations, aliases and
associations carry the writing run's owner and uniqueness is per (owner, …) —
two accounts can hold same-named entities without sharing them. Web users see only their own
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
    CREATE TABLE IF NOT EXISTS kg_alias(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT NOT NULL DEFAULT '',
        alias TEXT NOT NULL,
        target TEXT NOT NULL,
        ts TEXT NOT NULL,
        UNIQUE(owner, alias)
    );
    CREATE INDEX IF NOT EXISTS idx_alias_target ON kg_alias(owner, target);
    CREATE TABLE IF NOT EXISTS kg_association(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT NOT NULL DEFAULT '',
        src TEXT NOT NULL,
        dst TEXT NOT NULL,
        weight REAL NOT NULL DEFAULT 0,
        ts TEXT NOT NULL,
        UNIQUE(owner, src, dst)
    );
    CREATE INDEX IF NOT EXISTS idx_assoc_src ON kg_association(owner, src);
    CREATE INDEX IF NOT EXISTS idx_assoc_dst ON kg_association(owner, dst);
"""


def _ensure_schema(conn: sqlite3.Connection, legacy: str = "") -> None:
    # CREATE IF NOT EXISTS also upgrades already owner-scoped DBs with tables
    # added after the original schema. Legacy tables need their owner columns
    # before the owner-based indexes can be created.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(kg_entity)")}
    if not cols or "owner" in cols:
        conn.executescript(_SCHEMA)
        conn.commit()
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
    conn.executescript(_SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _attrs(value: str | None) -> dict:
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _canonical(conn: sqlite3.Connection, name: str, owner: str) -> str:
    """Resolve one owner-scoped alias chain, rejecting corrupted cycles."""
    current = name
    seen = set()
    while current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT target FROM kg_alias WHERE owner=? AND alias=?",
            (owner, current)).fetchone()
        if row is None:
            return current
        current = row[0]
    raise ValueError(f"alias cycle involving '{name}'")


def delete_owner(db_path: str, owner: str) -> int:
    """Delete every KG row owned by `owner` (account deletion cleanup —
    web/routes_admin.py). No-op when the DB file doesn't exist; ownerless ''
    rows (the CLI path) are untouched. Returns rows removed."""
    if not Path(db_path).exists():
        return 0
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        _ensure_schema(conn)
        removed = 0
        for table in ("kg_relation", "kg_alias", "kg_association", "kg_entity"):
            cur = conn.execute(f"DELETE FROM {table} WHERE owner = ?", (owner,))
            removed += cur.rowcount
        conn.commit()
        return removed
    finally:
        conn.close()


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
            name = _canonical(conn, str(e["name"]), owner)
            _upsert_entity(conn, name, str(e.get("type") or ""),
                           e.get("attrs"), owner)
        for r in relations:
            src = _canonical(conn, str(r["src"]), owner)
            dst = _canonical(conn, str(r["dst"]), owner)
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
        merged = _attrs(row["attrs"])
        merged.update(attrs or {})
        conn.execute(
            "UPDATE kg_entity SET type=?, attrs=?, ts=? WHERE owner=? AND name=?",
            (etype or row["type"] or "", json.dumps(merged), _now(), owner, name),
        )


def _merge_entity(conn: sqlite3.Connection, alias: str, target: str,
                  owner: str) -> int:
    """Merge an existing alias entity into target; return moved edge count."""
    source = conn.execute(
        "SELECT type, attrs FROM kg_entity WHERE owner=? AND name=?",
        (owner, alias)).fetchone()
    if source is None:
        return 0
    dest = conn.execute(
        "SELECT type, attrs FROM kg_entity WHERE owner=? AND name=?",
        (owner, target)).fetchone()
    merged = _attrs(source["attrs"])
    merged.update(_attrs(dest["attrs"]))  # canonical values win conflicts
    conn.execute(
        "UPDATE kg_entity SET type=?, attrs=?, ts=? WHERE owner=? AND name=?",
        (dest["type"] or source["type"] or "", json.dumps(merged), _now(), owner, target),
    )
    rows = conn.execute(
        "SELECT id, src, rel, dst, attrs FROM kg_relation "
        "WHERE owner=? AND (src=? OR dst=?)", (owner, alias, alias)).fetchall()
    for row in rows:
        src = target if row["src"] == alias else row["src"]
        dst = target if row["dst"] == alias else row["dst"]
        if src == dst:
            conn.execute("DELETE FROM kg_relation WHERE id=?", (row["id"],))
            continue
        existing = conn.execute(
            "SELECT id, attrs FROM kg_relation "
            "WHERE owner=? AND src=? AND rel=? AND dst=?",
            (owner, src, row["rel"], dst)).fetchone()
        if existing is None:
            conn.execute("UPDATE kg_relation SET src=?, dst=?, ts=? WHERE id=?",
                         (src, dst, _now(), row["id"]))
        else:
            edge_attrs = _attrs(row["attrs"])
            edge_attrs.update(_attrs(existing["attrs"]))
            conn.execute("UPDATE kg_relation SET attrs=?, ts=? WHERE id=?",
                         (json.dumps(edge_attrs), _now(), existing["id"]))
            conn.execute("DELETE FROM kg_relation WHERE id=?", (row["id"],))
    conn.execute("UPDATE kg_alias SET target=?, ts=? WHERE owner=? AND target=?",
                 (target, _now(), owner, alias))
    conn.execute("DELETE FROM kg_entity WHERE owner=? AND name=?", (owner, alias))
    return len(rows)


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
            name = _canonical(conn, args["name"], owner)
            _upsert_entity(conn, name, args.get("type", ""), args.get("attrs"), owner)
            conn.commit()
            row = conn.execute(
                "SELECT name, type, attrs, ts FROM kg_entity "
                "WHERE owner=? AND name=?", (owner, name)).fetchone()
            d = dict(row)
            d["attrs"] = _attrs(d["attrs"])
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
            src = _canonical(conn, args["src"], owner)
            dst = _canonical(conn, args["dst"], owner)
            _upsert_entity(conn, src, owner=owner)
            _upsert_entity(conn, dst, owner=owner)
            conn.execute(
                "INSERT INTO kg_relation(owner, src, rel, dst, attrs, ts) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(owner, src, rel, dst) DO UPDATE SET "
                "attrs=excluded.attrs, ts=excluded.ts",
                (owner, src, args["rel"], dst,
                 json.dumps(args.get("attrs") or {}), _now()),
            )
            conn.commit()
            return ToolResult(status="ok", result={
                "edge": f"{src} -[{args['rel']}]-> {dst}"})
        finally:
            conn.close()


class KgAlias(Tool):
    name = "kg.alias"
    description = ("Map an alternate entity name to a canonical entity. Future "
                  "reads and writes resolve the alias. merge=true also merges an "
                  "existing alias entity's attributes and relations (confirmation "
                  "required; canonical values win attribute conflicts).")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "alias": {"type": "string", "description": "Alternate name to resolve."},
            "target": {"type": "string", "description": "Canonical entity name."},
            "merge": {"type": "boolean", "default": False,
                      "description": "Merge an existing alias entity into target."},
        },
        "required": ["alias", "target"],
    }

    def needs_confirmation(self, args: dict, context: ToolContext) -> bool:
        return bool(args.get("merge"))

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        owner = scoped_owner(ctx)
        conn = _connect(ctx)
        try:
            alias = args["alias"]
            target = _canonical(conn, args["target"], owner)
            if alias == target:
                return ToolResult(status="error", result=None,
                                  error="alias resolves to itself or would create a cycle")
            exists = conn.execute(
                "SELECT 1 FROM kg_entity WHERE owner=? AND name=?",
                (owner, alias)).fetchone()
            if exists and not args.get("merge"):
                return ToolResult(status="error", result=None, error=(
                    "alias names an existing entity; use merge=true to combine it "
                    "with the canonical entity"))
            _upsert_entity(conn, target, owner=owner)
            moved = _merge_entity(conn, alias, target, owner) if args.get("merge") else 0
            conn.execute(
                "INSERT INTO kg_alias(owner, alias, target, ts) VALUES (?,?,?,?) "
                "ON CONFLICT(owner, alias) DO UPDATE SET "
                "target=excluded.target, ts=excluded.ts",
                (owner, alias, target, _now()))
            conn.commit()
            return ToolResult(status="ok", result={
                "alias": alias, "target": target, "merged": bool(args.get("merge")),
                "relations_merged": moved})
        finally:
            conn.close()


class KgAssociate(Tool):
    name = "kg.associate"
    description = ("Record entities recalled or used together. Strengthens each "
                  "undirected pair; later kg.neighbors calls return them as related "
                  "context, strongest first. Entities are auto-created and aliases "
                  "are resolved.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "entities": {"type": "array", "minItems": 2, "maxItems": 20,
                         "items": {"type": "string"},
                         "description": "Two or more entities from one recall/event."},
        },
        "required": ["entities"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        owner = scoped_owner(ctx)
        conn = _connect(ctx)
        try:
            names = []
            for raw in args["entities"]:
                name = _canonical(conn, raw, owner)
                if name not in names:
                    names.append(name)
            if len(names) < 2:
                return ToolResult(status="error", result=None,
                                  error="need at least two distinct entities")
            for name in names:
                _upsert_entity(conn, name, owner=owner)
            associations = []
            for i, left in enumerate(names):
                for right in names[i + 1:]:
                    src, dst = sorted((left, right))
                    conn.execute(
                        "INSERT INTO kg_association(owner, src, dst, weight, ts) "
                        "VALUES (?,?,?,1,?) ON CONFLICT(owner, src, dst) DO UPDATE SET "
                        "weight=kg_association.weight+1, ts=excluded.ts",
                        (owner, src, dst, _now()))
                    weight = conn.execute(
                        "SELECT weight FROM kg_association "
                        "WHERE owner=? AND src=? AND dst=?", (owner, src, dst)).fetchone()[0]
                    associations.append({"src": src, "dst": dst, "weight": weight})
            conn.commit()
            return ToolResult(status="ok", result={
                "entities": names, "associations": associations})
        finally:
            conn.close()


class KgQuery(Tool):
    name = "kg.query"
    description = ("Look up entities by name (exact or substring) and/or type. "
                  "Returns entities with attributes and aliases. Scoped to YOUR "
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
            "name": {"type": "string", "description": "Name, alias, or substring to match."},
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
            sql = f"SELECT owner, name, type, attrs, ts FROM kg_entity WHERE 1=1{frag} "
            if args.get("name"):
                name = args["name"]
                canonical = name if all_owners else _canonical(conn, name, scoped_owner(ctx))
                sql += "AND name = ? " if canonical != name else "AND name LIKE ? "
                params.append(canonical if canonical != name else f"%{name}%")
            if args.get("type"):
                sql += "AND type = ? "
                params.append(args["type"])
            sql += "ORDER BY name LIMIT ?"
            params.append(int(args.get("limit", 25)))
            rows = []
            for r in conn.execute(sql, params).fetchall():
                d = dict(r)
                owner = d.pop("owner")
                d["attrs"] = _attrs(d["attrs"])
                d["aliases"] = [row[0] for row in conn.execute(
                    "SELECT alias FROM kg_alias WHERE owner=? AND target=? ORDER BY alias",
                    (owner, d["name"])).fetchall()]
                rows.append(d)
            return ToolResult(status="ok", result={"entities": rows, "count": len(rows)})
        finally:
            conn.close()


class KgNeighbors(Tool):
    name = "kg.neighbors"
    description = ("Return the subgraph around an entity: outgoing and incoming "
                  "relations up to `depth` hops plus weight-sorted related context. "
                  "Aliases resolve to the canonical root. Scoped to YOUR relations; "
                  "all_owners=true is an admin/debug escape hatch that traverses "
                  "every user's graph.")
    private = True
    read_only = True

    def needs_confirmation(self, args: dict, context: ToolContext) -> bool:
        # all_owners lifts the owner filter (audit finding 3).
        return bool(args.get("all_owners"))

    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Entity or alias to expand from."},
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
            start = (args["name"] if all_owners else
                     _canonical(conn, args["name"], scoped_owner(ctx)))
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
            related = []
            for row in conn.execute(
                    f"SELECT src, dst, weight FROM kg_association "
                    f"WHERE (src=? OR dst=?){frag} ORDER BY weight DESC, src, dst",
                    (start, start, *fparams)).fetchall():
                related.append({
                    "name": row["dst"] if row["src"] == start else row["src"],
                    "weight": row["weight"]})
            return ToolResult(status="ok", result={
                "root": start,
                "nodes": sorted(seen),
                "edges": edges,
                "edge_count": len(edges),
                "related": related,
            })
        finally:
            conn.close()


class KgRemoveRelation(Tool):
    name = "kg.remove_relation"
    description = "Remove a specific relation src -[rel]-> dst. Aliases are resolved."
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
            owner = scoped_owner(ctx)
            src = _canonical(conn, args["src"], owner)
            dst = _canonical(conn, args["dst"], owner)
            frag, params = owner_clause(ctx)
            cur = conn.execute(
                f"DELETE FROM kg_relation WHERE src=? AND rel=? AND dst=?{frag}",
                (src, args["rel"], dst, *params),
            )
            conn.commit()
            if cur.rowcount == 0:
                return ToolResult(status="error", result=None, error="no such relation")
            return ToolResult(status="ok", result={
                "removed": f"{src} -[{args['rel']}]-> {dst}"})
        finally:
            conn.close()
