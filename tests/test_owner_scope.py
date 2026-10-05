"""Owner scoping for the knowledge stores (audit 2026-10-05 finding 3).

memory/kg/rag rows carry the writing run's owner: one account's reads never
return another's entries, all_owners=true is the confirmation-gated escape
(same mechanics as trace.query/trace.mine), the ownerless CLI path is
unfiltered, and rows from before the owner column migrate to the first
admin account in the users DB. pageindex storage is per-account.
"""
import asyncio
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from runtime.owner_scope import legacy_owner
from runtime.tool_base import ToolContext
from tools.kg.graph import KgAddRelation, KgNeighbors, KgQuery, KgUpsertEntity
from tools.memory.store import (
    MemoryAppend,
    MemoryDelete,
    MemoryGet,
    MemoryList,
    MemorySearch,
)
from tools.rag import store as rag_store
from tools.rag.store import RagCollections, RagDelete, RagIndex, RagSearch


def _run(tool, args, ctx):
    return asyncio.run(tool.execute(args, ctx))


def _ctx(tmp_path, owner=None, extra_cfg=None):
    cfg = {"tools": {"memory": {"db_path": str(tmp_path / "mem.db")},
                     "rag": {"db_path": str(tmp_path / "rag.db")}}}
    cfg.update(extra_cfg or {})
    return ToolContext(request_id="t", budget=None, config=cfg, owner=owner)


def _users_db(tmp_path, admin="root"):
    db = tmp_path / "users.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users(username TEXT, is_admin INTEGER)")
    conn.execute("INSERT INTO users VALUES ('root', 1)")
    conn.execute("INSERT INTO users VALUES ('bob', 0)")
    conn.commit()
    conn.close()
    return db


# ---- memory ---------------------------------------------------------------

def test_memory_owner_isolation_and_escape(tmp_path):
    alice = _ctx(tmp_path, "alice")
    bob = _ctx(tmp_path, "bob")
    cli = _ctx(tmp_path)
    aid = _run(MemoryAppend(), {"content": "alice secret note"}, alice).result["id"]
    _run(MemoryAppend(), {"content": "bob note"}, bob)

    # Each sees only their own; the ownerless path sees everything.
    assert _run(MemorySearch(), {"query": "secret"}, bob).result["count"] == 0
    assert _run(MemorySearch(), {"query": "secret"}, alice).result["count"] == 1
    assert _run(MemoryList(), {}, bob).result["count"] == 1
    assert _run(MemoryList(), {}, cli).result["count"] == 2
    # all_owners is the escape hatch.
    r = _run(MemorySearch(), {"query": "secret", "all_owners": True}, bob)
    assert r.result["count"] == 1
    # get/delete are owner-filtered too (no id-fishing across accounts).
    assert _run(MemoryGet(), {"id": aid}, bob).status == "error"
    assert _run(MemoryDelete(), {"id": aid}, bob).status == "error"
    assert _run(MemoryGet(), {"id": aid}, alice).status == "ok"


def test_all_owners_is_confirmation_gated():
    for tool in (MemorySearch(), MemoryList(), KgQuery(), KgNeighbors(),
                 RagSearch(), RagCollections()):
        assert tool.needs_confirmation({"all_owners": True}, None) is True
        assert tool.needs_confirmation({}, None) is False


# ---- kg -------------------------------------------------------------------

def test_kg_same_name_entities_are_per_owner(tmp_path):
    alice = _ctx(tmp_path, "alice")
    bob = _ctx(tmp_path, "bob")
    _run(KgUpsertEntity(), {"name": "model-x", "attrs": {"v": 1}}, alice)
    _run(KgUpsertEntity(), {"name": "model-x", "attrs": {"v": 2}}, bob)
    _run(KgAddRelation(), {"src": "model-x", "rel": "runs_on",
                           "dst": "gpu0"}, alice)

    qa = _run(KgQuery(), {"name": "model-x"}, alice).result["entities"]
    qb = _run(KgQuery(), {"name": "model-x"}, bob).result["entities"]
    assert qa[0]["attrs"]["v"] == 1 and qb[0]["attrs"]["v"] == 2
    # A's edge is invisible to B's traversal; all_owners lifts it.
    na = _run(KgNeighbors(), {"name": "model-x"}, alice).result
    nb = _run(KgNeighbors(), {"name": "model-x"}, bob).result
    assert na["edge_count"] == 1 and nb["edge_count"] == 0
    nall = _run(KgNeighbors(), {"name": "model-x", "all_owners": True}, bob)
    assert nall.result["edge_count"] == 1


# ---- rag ------------------------------------------------------------------

@pytest.fixture
def rag(tmp_path, monkeypatch):
    async def fake_embed(texts, _ctx):
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(rag_store, "_embed", fake_embed)
    return tmp_path


def test_rag_owner_isolation_and_escape(rag):
    alice = _ctx(rag, "alice")
    bob = _ctx(rag, "bob")
    r = _run(RagIndex(), {"collection": "notes", "text": "alice chunk"}, alice)
    assert r.status == "ok" and r.result["chunks_indexed"] == 1

    assert _run(RagCollections(), {}, bob).result["collections"] == []
    rb = _run(RagSearch(), {"query": "chunk", "collection": "notes"}, bob)
    assert rb.result["count"] == 0
    ra = _run(RagSearch(), {"query": "chunk", "collection": "notes"}, alice)
    assert ra.result["count"] == 1
    rall = _run(RagSearch(), {"query": "chunk", "all_owners": True}, bob)
    assert rall.result["count"] == 1
    # delete is owner-filtered: bob can't wipe alice's collection.
    rd = _run(RagDelete(), {"collection": "notes"}, bob)
    assert rd.result["deleted_chunks"] == 0
    assert _run(RagCollections(), {}, alice).result["collections"] != []


# ---- legacy migration ------------------------------------------------------

def test_memory_legacy_rows_migrate_to_first_admin(tmp_path):
    users = _users_db(tmp_path)
    db = tmp_path / "mem.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE memory("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
                 "epoch REAL NOT NULL, kind TEXT DEFAULT 'note',"
                 "tags TEXT DEFAULT '', content TEXT NOT NULL,"
                 "source TEXT DEFAULT '')")
    conn.execute("INSERT INTO memory(ts, epoch, content) "
                 "VALUES ('2026-01-01', 0, 'legacy fact')")
    conn.commit()
    conn.close()

    cfg = {"web": {"users_db": str(users)}}
    root = _ctx(tmp_path, "root", cfg)
    bob = _ctx(tmp_path, "bob", cfg)
    # The pre-scoping row lands with the first admin — reachable by that
    # user, invisible to others.
    contents = [e["content"] for e in
                _run(MemoryList(), {}, root).result["entries"]]
    assert "legacy fact" in contents
    assert _run(MemorySearch(), {"query": "legacy"}, bob).result["count"] == 0


def test_kg_legacy_tables_rebuild_with_owner(tmp_path):
    users = _users_db(tmp_path)
    db = tmp_path / "mem.db"   # kg defaults to the memory DB path
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE kg_entity(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL, type TEXT DEFAULT '',
            attrs TEXT DEFAULT '{}', ts TEXT NOT NULL);
        CREATE TABLE kg_relation(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL,
            attrs TEXT DEFAULT '{}', ts TEXT NOT NULL,
            UNIQUE(src, rel, dst));
        INSERT INTO kg_entity(name, ts) VALUES ('legacy-model', '2026-01-01');
    """)
    conn.commit()
    conn.close()

    cfg = {"web": {"users_db": str(users)}}
    root = _ctx(tmp_path, "root", cfg)
    bob = _ctx(tmp_path, "bob", cfg)
    ents = _run(KgQuery(), {"name": "legacy"}, root).result["entities"]
    assert len(ents) == 1
    assert _run(KgQuery(), {"name": "legacy"}, bob).result["count"] == 0
    # Per-owner uniqueness after the rebuild: bob can create the same name
    # (the old GLOBAL unique would have raised or merged into root's row).
    r = _run(KgUpsertEntity(), {"name": "legacy-model"}, bob)
    assert r.status == "ok"


def test_rag_legacy_rows_migrate_to_first_admin(tmp_path):
    users = _users_db(tmp_path)
    db = tmp_path / "rag.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE rag_doc("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 "collection TEXT NOT NULL, source TEXT DEFAULT '',"
                 "chunk_idx INTEGER DEFAULT 0, text TEXT NOT NULL,"
                 "dim INTEGER NOT NULL, embedding BLOB NOT NULL,"
                 "ts TEXT NOT NULL, hash TEXT)")
    conn.execute("INSERT INTO rag_doc(collection, text, dim, embedding, ts, hash)"
                 " VALUES ('legacy', 'old chunk', 1, x'0000803f', '2026-01-01', 'h1')")
    conn.commit()
    conn.close()

    cfg = {"web": {"users_db": str(users)}}
    root = _ctx(tmp_path, "root", cfg)
    bob = _ctx(tmp_path, "bob", cfg)
    cols = _run(RagCollections(), {}, root).result["collections"]
    assert [c["collection"] for c in cols] == ["legacy"]
    assert _run(RagCollections(), {}, bob).result["collections"] == []


def test_legacy_owner_helper(tmp_path):
    users = _users_db(tmp_path)
    assert legacy_owner({"web": {"users_db": str(users)}}) == "root"
    # Undeterminable (no users DB at the configured path) -> ''.
    assert legacy_owner({"web": {"users_db": str(tmp_path / "nope.db")}}) == ""


# ---- pageindex storage scoping ---------------------------------------------

PI_DIR = Path(__file__).resolve().parent.parent / "plugins" / "pageindex"


def _load_client():
    name = "pageindex_client_scope"
    spec = importlib.util.spec_from_file_location(name, PI_DIR / "client.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_pageindex_storage_is_per_owner(monkeypatch, tmp_path):
    from runtime import paths
    monkeypatch.setattr(paths, "DATA", tmp_path)
    mod = _load_client()
    assert mod.settings({}, owner="alice")["storage_path"] == \
        str(tmp_path / "pageindex" / "alice")
    assert mod.settings({})["storage_path"] == str(tmp_path / "pageindex")
    # A path-hostile owner is sanitized to a single safe segment.
    p = mod.settings({}, owner="../e v i l")["storage_path"]
    assert Path(p).parent == tmp_path / "pageindex"
    assert ".." not in Path(p).name
    # An explicit storage_path always wins (operator choice).
    s = mod.settings({"plugins": {"pageindex": {"storage_path": "/tmp/pi"}}},
                     owner="alice")
    assert s["storage_path"] == "/tmp/pi"
    sys.modules.pop("pageindex_client_scope", None)
