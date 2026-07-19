"""kg.* tools: entity upsert/merge, relation edges, neighbor traversal, removal.

Every test points the tool at a throwaway SQLite DB under tmp_path via the
tools.kg.db_path config key — no /srv writes, no shared state between tests.
"""
import asyncio

import pytest

from tools.kg.graph import (KgUpsertEntity, KgAddRelation, KgQuery,
                            KgNeighbors, KgRemoveRelation)
from runtime.tool_base import ToolContext


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(
        request_id="t", budget=None,
        config={"tools": {"kg": {"db_path": str(tmp_path / "kg.db")}}})


def _run(tool, args, ctx):
    return asyncio.run(tool.execute(args, ctx))


def _edge(src, rel, dst):
    return {"src": src, "rel": rel, "dst": dst}


def test_upsert_creates_then_merges_attrs(ctx):
    r = _run(KgUpsertEntity(), {"name": "qwen3-35b", "type": "model",
                                "attrs": {"params": "35B"}}, ctx)
    assert r.status == "ok"
    assert r.result["name"] == "qwen3-35b" and r.result["type"] == "model"
    assert r.result["attrs"] == {"params": "35B"}
    # second upsert: new attrs merge in, type is preserved when omitted
    r = _run(KgUpsertEntity(), {"name": "qwen3-35b",
                                "attrs": {"quant": "Q4_K_L"}}, ctx)
    assert r.result["attrs"] == {"params": "35B", "quant": "Q4_K_L"}
    assert r.result["type"] == "model"


def test_add_relation_autocreates_entities(ctx):
    r = _run(KgAddRelation(), {"src": "qwen3-35b", "rel": "quantized_as",
                               "dst": "qwen3-35b-Q4_K_L"}, ctx)
    assert r.status == "ok"
    assert r.result["edge"] == "qwen3-35b -[quantized_as]-> qwen3-35b-Q4_K_L"
    q = _run(KgQuery(), {"name": "qwen3-35b"}, ctx)
    names = [e["name"] for e in q.result["entities"]]
    assert "qwen3-35b" in names and "qwen3-35b-Q4_K_L" in names


def test_query_by_substring_and_type(ctx):
    _run(KgUpsertEntity(), {"name": "qwen3-35b", "type": "model"}, ctx)
    _run(KgUpsertEntity(), {"name": "glm-5.2", "type": "model"}, ctx)
    _run(KgUpsertEntity(), {"name": "qwen-papers", "type": "dataset"}, ctx)
    r = _run(KgQuery(), {"name": "qwen"}, ctx)
    assert [e["name"] for e in r.result["entities"]] == ["qwen-papers", "qwen3-35b"]
    r = _run(KgQuery(), {"type": "model"}, ctx)
    assert {e["name"] for e in r.result["entities"]} == {"qwen3-35b", "glm-5.2"}
    assert r.result["count"] == 2
    r = _run(KgQuery(), {"name": "qwen", "type": "dataset"}, ctx)
    assert [e["name"] for e in r.result["entities"]] == ["qwen-papers"]


def test_same_relation_twice_updates_not_duplicates(ctx):
    for note in ("first", "second"):
        _run(KgAddRelation(), {"src": "job-7", "rel": "produced", "dst": "ckpt-3",
                               "attrs": {"note": note}}, ctx)
    n = _run(KgNeighbors(), {"name": "job-7"}, ctx)
    assert n.result["edge_count"] == 1        # UNIQUE(src, rel, dst) upsert


def test_neighbors_traverses_both_directions_and_depth(ctx):
    # chain: D -[feeds]-> A -[trained_on]-> B -[derived_from]-> C
    _run(KgAddRelation(), {"src": "D", "rel": "feeds", "dst": "A"}, ctx)
    _run(KgAddRelation(), {"src": "A", "rel": "trained_on", "dst": "B"}, ctx)
    _run(KgAddRelation(), {"src": "B", "rel": "derived_from", "dst": "C"}, ctx)

    n1 = _run(KgNeighbors(), {"name": "A", "depth": 1}, ctx).result
    assert n1["root"] == "A"
    assert set(n1["nodes"]) == {"A", "B", "D"}            # incoming and outgoing
    assert _edge("D", "feeds", "A") in n1["edges"]
    assert _edge("A", "trained_on", "B") in n1["edges"]

    n2 = _run(KgNeighbors(), {"name": "A", "depth": 2}, ctx).result
    assert set(n2["nodes"]) == {"A", "B", "C", "D"}       # second hop reaches C
    assert n2["edge_count"] == 3


def test_remove_relation(ctx):
    _run(KgAddRelation(), {"src": "A", "rel": "likes", "dst": "B"}, ctx)
    r = _run(KgRemoveRelation(), {"src": "A", "rel": "likes", "dst": "B"}, ctx)
    assert r.status == "ok" and r.result["removed"] == "A -[likes]-> B"
    n = _run(KgNeighbors(), {"name": "A"}, ctx)
    assert n.result["edge_count"] == 0
    # entities survive the edge removal; only the relation is gone
    q = _run(KgQuery(), {"name": "A"}, ctx)
    assert q.result["count"] == 1
    # removing a non-existent edge is an explicit error
    r = _run(KgRemoveRelation(), {"src": "A", "rel": "likes", "dst": "B"}, ctx)
    assert r.status == "error" and "no such relation" in r.error
