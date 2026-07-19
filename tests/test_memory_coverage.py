"""memory.* tools: append → search (FTS5) → get/list → delete round-trips.

Every test points the tool at a throwaway SQLite DB under tmp_path via the
tools.memory.db_path config key — no /srv writes, no shared state. FTS5 is
present in the project venv's sqlite3, so the FTS (not LIKE) path is exercised.
"""
import asyncio

import pytest

from tools.memory.store import (MemoryAppend, MemorySearch, MemoryGet,
                                MemoryList, MemoryDelete)
from runtime.tool_base import ToolContext


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(
        request_id="t", budget=None,
        config={"tools": {"memory": {"db_path": str(tmp_path / "mem.db")}}})


def _run(tool, args, ctx):
    return asyncio.run(tool.execute(args, ctx))


def _append(ctx, content, **kw):
    r = _run(MemoryAppend(), {"content": content, **kw}, ctx)
    assert r.status == "ok"
    return r.result["id"]


def test_append_then_get_round_trip(ctx):
    mid = _append(ctx, "The R9700 has 32GB of VRAM",
                  kind="fact", tags="gpu vram", source="run-1")
    r = _run(MemoryGet(), {"id": mid}, ctx)
    assert r.status == "ok"
    assert r.result["content"] == "The R9700 has 32GB of VRAM"
    assert r.result["kind"] == "fact"
    assert r.result["tags"] == "gpu vram"
    assert r.result["source"] == "run-1"
    assert r.result["ts"]
    # unknown id is an explicit error, not an empty result
    r = _run(MemoryGet(), {"id": 9999}, ctx)
    assert r.status == "error" and "no memory with id 9999" in r.error


def test_append_then_fts_search_finds(ctx):
    _append(ctx, "The R9700 has 32GB of VRAM")
    _append(ctx, "The bakery makes good bread")
    r = _run(MemorySearch(), {"query": "VRAM"}, ctx)
    assert r.status == "ok" and r.result["count"] == 1
    assert "R9700" in r.result["matches"][0]["content"]


def test_fts_boolean_syntax(ctx):
    _append(ctx, "The R9700 GPU has 32GB of VRAM")
    _append(ctx, "The bakery makes good bread")
    r = _run(MemorySearch(), {"query": "R9700 AND VRAM"}, ctx)
    assert r.result["count"] == 1
    r = _run(MemorySearch(), {"query": "R9700 AND bakery"}, ctx)
    assert r.result["count"] == 0
    r = _run(MemorySearch(), {"query": "R9700 OR bakery"}, ctx)
    assert r.result["count"] == 2
    # malformed FTS syntax must not crash: it falls through to LIKE (no match)
    r = _run(MemorySearch(), {"query": "AND OR ("}, ctx)
    assert r.status == "ok" and r.result["count"] == 0


def test_search_kind_filter_and_limit(ctx):
    _append(ctx, "alpha gpu note", kind="note")
    _append(ctx, "beta gpu fact", kind="fact")
    _append(ctx, "gamma gpu fact", kind="fact")
    r = _run(MemorySearch(), {"query": "gpu", "kind": "fact"}, ctx)
    assert r.result["count"] == 2
    assert all(m["kind"] == "fact" for m in r.result["matches"])
    r = _run(MemorySearch(), {"query": "gpu", "limit": 1}, ctx)
    assert r.result["count"] == 1


def test_search_truncates_but_get_returns_full(ctx):
    mid = _append(ctx, "gpu " * 250)                    # 1000 chars of 'gpu'
    r = _run(MemorySearch(), {"query": "gpu"}, ctx)
    m = r.result["matches"][0]
    assert len(m["content"]) == 601 and m["content"].endswith("…")
    full = _run(MemoryGet(), {"id": mid}, ctx)
    assert len(full.result["content"]) == 1000


def test_list_recent_and_kind_filter(ctx):
    ids = {_append(ctx, "first note", kind="note"),
           _append(ctx, "second fact", kind="fact"),
           _append(ctx, "third note", kind="note")}
    r = _run(MemoryList(), {}, ctx)
    assert r.status == "ok" and r.result["count"] == 3
    assert {e["id"] for e in r.result["entries"]} == ids
    r = _run(MemoryList(), {"kind": "note"}, ctx)
    assert r.result["count"] == 2
    assert all(e["kind"] == "note" for e in r.result["entries"])
    r = _run(MemoryList(), {"limit": 1}, ctx)
    assert r.result["count"] == 1


def test_delete_removes_from_store_and_index(ctx):
    mid = _append(ctx, "temporary scratch about quokkas")
    r = _run(MemoryDelete(), {"id": mid}, ctx)
    assert r.status == "ok" and r.result["deleted"] == mid
    # gone from the base table …
    assert _run(MemoryGet(), {"id": mid}, ctx).status == "error"
    # … and from the FTS index (the delete trigger fires)
    r = _run(MemorySearch(), {"query": "quokkas"}, ctx)
    assert r.result["count"] == 0
    # deleting twice is an explicit error
    r = _run(MemoryDelete(), {"id": mid}, ctx)
    assert r.status == "error" and "no memory with id" in r.error
