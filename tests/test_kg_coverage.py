"""kg.* tools: entity upsert/merge, relation edges, neighbor traversal, removal.

Every test points the tool at a throwaway SQLite DB under tmp_path via the
tools.kg.db_path config key — no /srv writes, no shared state between tests.
"""
import asyncio

import pytest

from runtime.tool_base import ToolContext
from tools.kg.graph import (
    KgAddRelation,
    KgAlias,
    KgAssociate,
    KgNeighbors,
    KgQuery,
    KgRemoveRelation,
    KgUpsertEntity,
)


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


def test_alias_resolves_reads_writes_and_relation_removal(ctx):
    _run(KgUpsertEntity(), {"name": "Qwen3", "type": "model"}, ctx)
    tool = KgAlias()
    assert tool.needs_confirmation({"merge": False}, ctx) is False
    assert tool.needs_confirmation({"merge": True}, ctx) is True
    assert _run(tool, {"alias": "qwen", "target": "Qwen3"}, ctx).status == "ok"

    _run(KgUpsertEntity(), {"name": "qwen", "attrs": {"params": "35B"}}, ctx)
    edge = _run(KgAddRelation(), {"src": "qwen", "rel": "runs_on", "dst": "R9700"}, ctx)
    assert edge.result["edge"] == "Qwen3 -[runs_on]-> R9700"
    query = _run(KgQuery(), {"name": "qwen"}, ctx).result
    assert query["count"] == 1
    assert query["entities"][0]["name"] == "Qwen3"
    assert query["entities"][0]["aliases"] == ["qwen"]
    assert query["entities"][0]["attrs"] == {"params": "35B"}
    neighbors = _run(KgNeighbors(), {"name": "qwen"}, ctx).result
    assert neighbors["root"] == "Qwen3" and neighbors["edge_count"] == 1
    removed = _run(KgRemoveRelation(), {"src": "qwen", "rel": "runs_on",
                                       "dst": "R9700"}, ctx)
    assert removed.status == "ok"


def test_alias_merge_deduplicates_edges_and_rejects_cycles(ctx):
    _run(KgUpsertEntity(), {"name": "canonical", "type": "model",
                            "attrs": {"shared": "canonical"}}, ctx)
    _run(KgUpsertEntity(), {"name": "alternate", "type": "ignored",
                            "attrs": {"shared": "alias", "extra": 1}}, ctx)
    for src, dst, note in (("canonical", "C", "canonical"),
                           ("alternate", "C", "alias"),
                           ("alternate", "D", "alias"),
                           ("canonical", "alternate", "link")):
        _run(KgAddRelation(), {"src": src, "rel": "links", "dst": dst,
                               "attrs": {"note": note}}, ctx)

    tool = KgAlias()
    refused = _run(tool, {"alias": "alternate", "target": "canonical"}, ctx)
    assert refused.status == "error" and "merge=true" in refused.error
    merged = _run(tool, {"alias": "alternate", "target": "canonical",
                         "merge": True}, ctx)
    assert merged.status == "ok" and merged.result["relations_merged"] == 3
    query = _run(KgQuery(), {"name": "alternate"}, ctx).result
    assert query["count"] == 1
    assert query["entities"][0]["attrs"] == {"shared": "canonical", "extra": 1}
    assert query["entities"][0]["type"] == "model"
    neighbors = _run(KgNeighbors(), {"name": "canonical"}, ctx).result
    assert neighbors["edge_count"] == 2       # duplicate and self-link removed

    assert _run(tool, {"alias": "a", "target": "b"}, ctx).status == "ok"
    cycle = _run(tool, {"alias": "b", "target": "a"}, ctx)
    assert cycle.status == "error" and "cycle" in cycle.error


def test_associations_strengthen_and_return_related_context(ctx):
    _run(KgAlias(), {"alias": "qwen", "target": "Qwen3"}, ctx)
    tool = KgAssociate()
    first = _run(tool, {"entities": ["qwen", "R9700", "RAG"]}, ctx)
    assert first.status == "ok" and len(first.result["associations"]) == 3
    _run(tool, {"entities": ["Qwen3", "R9700"]}, ctx)
    related = _run(KgNeighbors(), {"name": "qwen"}, ctx).result["related"]
    assert related == [{"name": "R9700", "weight": 2.0},
                       {"name": "RAG", "weight": 1.0}]
    duplicate = _run(tool, {"entities": ["qwen", "Qwen3"]}, ctx)
    assert duplicate.status == "error"
