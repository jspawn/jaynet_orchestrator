"""Knowledge-surface bridge: graph.seed_kg (project graph → curated kg) and
the rag_excerpt hook (project graph → rag.search results)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

from runtime import hooks
from tests.conftest import run
from tools.kg import graph as kg_graph
from tools.rag import store as rag_store

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "graphify"


def _import(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


graph_tools = _import("gf_bridge_tools_test", PLUGIN_DIR / "tools" / "graph.py")
hooks_mod = _import("gf_bridge_hooks_test", PLUGIN_DIR / "hooks.py")
runner = hooks_mod.runner                    # the shared runner instance


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear()
    yield
    hooks.clear()


_GRAPH = {
    "nodes": [
        {"id": "app_widget", "kind": "class", "file": "app.py", "community": 1},
        {"id": "app_greet", "kind": "function", "file": "app.py", "community": 1},
        {"id": "use_main", "kind": "function", "file": "sub/use.py",
         "community": 2},
    ],
    "edges": [
        {"source": "app_widget", "target": "app_greet", "relation": "calls",
         "confidence": "EXTRACTED"},
        {"source": "use_main", "target": "app_greet", "relation": "calls",
         "confidence": "INFERRED"},
    ],
}


def _mk_project(projects, owner="u", pid="p1", graph=None):
    d = projects / owner / pid
    (d / "files").mkdir(parents=True)
    (d / "project.json").write_text('{"id": "p1"}')
    if graph is not None:
        root = runner.graph_root(projects, owner, pid)
        root.mkdir(parents=True)
        (root / "graph.json").write_text(json.dumps(graph))
    return d


def _kg_ctx(ctx, tmp_path, projects, **over):
    return ctx(config={
        "web": {"projects_dir": str(projects)},
        "tools": {"kg": {"db_path": str(tmp_path / "kg.db")}},
    }, **over)


# ---- load_graph ----------------------------------------------------------------

def test_load_graph_accepts_links_variant_and_garbage(tmp_path):
    (tmp_path / "graph.json").write_text(json.dumps(
        {"nodes": [{"id": "a"}], "links": [{"source": "a", "target": "a"}]}))
    g = runner.load_graph(tmp_path)
    assert g["edges"] == [{"source": "a", "target": "a"}]
    (tmp_path / "graph.json").write_text("garbage")
    assert runner.load_graph(tmp_path) is None


# ---- graph.seed_kg ---------------------------------------------------------------

def test_seed_kg_namespaces_with_provenance_and_merges(tmp_path, ctx):
    projects = tmp_path / "projects"
    _mk_project(projects, graph=_GRAPH)
    c = _kg_ctx(ctx, tmp_path, projects, owner="u", project_id="p1")
    res = run(graph_tools.GraphSeedKg().execute({}, c))
    assert res.status == "ok"
    assert res.result["entities"] == 3 and res.result["relations"] == 2

    q = run(kg_graph.KgQuery().execute({"name": "p1/"}, c))
    ents = {e["name"]: e for e in q.result["entities"]}
    assert set(ents) == {"p1/app_widget", "p1/app_greet", "p1/use_main"}
    assert ents["p1/app_widget"]["type"] == "class"
    assert ents["p1/app_widget"]["attrs"]["origin"] == "graphify"
    assert ents["p1/app_widget"]["attrs"]["project"] == "p1"

    n = run(kg_graph.KgNeighbors().execute({"name": "p1/app_greet"}, c))
    assert n.result["edge_count"] == 2
    # re-seeding upserts — no duplicate entities or relations
    run(graph_tools.GraphSeedKg().execute({}, c))
    q2 = run(kg_graph.KgQuery().execute({"name": "p1/"}, c))
    assert q2.result["count"] == 3
    n2 = run(kg_graph.KgNeighbors().execute({"name": "p1/app_greet"}, c))
    assert n2.result["edge_count"] == 2


def test_seed_kg_kinds_filter_keeps_subgraph_closed(tmp_path, ctx):
    projects = tmp_path / "projects"
    _mk_project(projects, graph=_GRAPH)
    c = _kg_ctx(ctx, tmp_path, projects, owner="u", project_id="p1")
    res = run(graph_tools.GraphSeedKg().execute({"kinds": ["class"]}, c))
    assert res.result["entities"] == 1
    assert res.result["relations"] == 0     # no typeless endpoint stubs


def test_seed_kg_requires_project(tmp_path, ctx):
    c = _kg_ctx(ctx, tmp_path, tmp_path / "projects")
    res = run(graph_tools.GraphSeedKg().execute({}, c))
    assert res.status == "error" and "no project" in res.error


def test_seed_kg_flows_wiki_nodes(tmp_path, ctx):
    """Wiki-extractor nodes ride the same seed path with type 'wiki'."""
    projects = tmp_path / "projects"
    d = _mk_project(projects, graph=_GRAPH)
    wiki = d / "files" / "wiki"
    wiki.mkdir()
    (wiki / "index.md").write_text("# Home\n\nSee [A](a.md).\n")
    (wiki / "a.md").write_text("# A\n")
    root = runner.graph_root(projects, "u", "p1")
    runner.augment_with_wiki(root / "graph.json", wiki)
    c = _kg_ctx(ctx, tmp_path, projects, owner="u", project_id="p1")
    res = run(graph_tools.GraphSeedKg().execute({}, c))
    assert res.status == "ok"
    q = run(kg_graph.KgQuery().execute({"name": "p1/wiki/"}, c))
    types = {e["name"]: e["type"] for e in q.result["entities"]}
    assert types == {"p1/wiki/index": "wiki", "p1/wiki/a": "wiki"}


# ---- rag_excerpt -----------------------------------------------------------------

def _seed_rag_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rag_doc(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "collection TEXT NOT NULL, source TEXT DEFAULT '', "
        "chunk_idx INTEGER DEFAULT 0, text TEXT NOT NULL, dim INTEGER NOT NULL, "
        "embedding BLOB NOT NULL, ts TEXT NOT NULL, hash TEXT)")
    def emb(v):
        return np.asarray(v, dtype=np.float32).tobytes()
    conn.execute("INSERT INTO rag_doc(collection, source, text, dim, embedding, ts)"
                 " VALUES ('proj', 'app.py', 'class Widget renders', 2, ?, '')",
                 (emb([1.0, 0.0]),))
    conn.execute("INSERT INTO rag_doc(collection, source, text, dim, embedding, ts)"
                 " VALUES ('proj', 'other.txt', 'unrelated content', 2, ?, '')",
                 (emb([0.0, 1.0]),))
    conn.commit()
    conn.close()


def _rag_ctx(ctx, tmp_path, projects, **over):
    _seed_rag_db(tmp_path / "rag.db")
    return ctx(config={
        "web": {"projects_dir": str(projects)},
        "tools": {"rag": {"db_path": str(tmp_path / "rag.db")}},
    }, **over)


@pytest.fixture
def _fake_embed(monkeypatch):
    async def fake(texts, ctx):
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(rag_store, "_embed", fake)


def test_rag_search_surfaces_graph_excerpt(tmp_path, ctx, _fake_embed):
    projects = tmp_path / "projects"
    _mk_project(projects, graph=_GRAPH)
    hooks.register("rag_excerpt", hooks_mod.rag_excerpt)
    c = _rag_ctx(ctx, tmp_path, projects, owner="u", project_id="p1")
    res = run(rag_store.RagSearch().execute({"query": "widget"}, c))
    assert res.status == "ok"
    excerpt = res.result.get("graph_excerpt") or ""
    assert "app_widget -[calls]-> app_greet" in excerpt


def test_rag_search_excerpt_scoping(tmp_path, ctx, _fake_embed):
    """The excerpt only ever comes from the RUN's owner+project: no bound
    project → no fire; another owner's project dir is unreachable."""
    projects = tmp_path / "projects"
    _mk_project(projects, graph=_GRAPH)
    hooks.register("rag_excerpt", hooks_mod.rag_excerpt)

    c = _rag_ctx(ctx, tmp_path, projects, owner="u")      # no project_id
    res = run(rag_store.RagSearch().execute({"query": "widget"}, c))
    assert "graph_excerpt" not in res.result

    c = _rag_ctx(ctx, tmp_path, projects, owner="v", project_id="p1")
    res = run(rag_store.RagSearch().execute({"query": "widget"}, c))
    assert "graph_excerpt" not in res.result      # v/p1 has no graph


def test_rag_search_without_plugin_is_plain(tmp_path, ctx, _fake_embed):
    """Plugin absent (no hook registered) → plain chunk hits, no key."""
    projects = tmp_path / "projects"
    _mk_project(projects, graph=_GRAPH)
    c = _rag_ctx(ctx, tmp_path, projects, owner="u", project_id="p1")
    res = run(rag_store.RagSearch().execute({"query": "widget"}, c))
    assert res.status == "ok"
    assert "graph_excerpt" not in res.result
    assert res.result["count"] >= 1


def test_rag_excerpt_graph_cache(tmp_path, ctx, _fake_embed, monkeypatch):
    """The excerpt hook fires on the request path: graph.json is parsed once
    and cached by (mtime, size) — the file is swapped atomically on rebuild,
    so a rebuild invalidates naturally and the next search sees the new
    graph."""
    projects = tmp_path / "projects"
    _mk_project(projects, graph=_GRAPH)
    hooks_mod._GRAPH_CACHE.clear()
    hooks.register("rag_excerpt", hooks_mod.rag_excerpt)
    calls = {"n": 0}
    real = hooks_mod.runner.load_graph
    def counting(groot):
        calls["n"] += 1
        return real(groot)
    monkeypatch.setattr(hooks_mod.runner, "load_graph", counting)

    c = _rag_ctx(ctx, tmp_path, projects, owner="u", project_id="p1")
    res = run(rag_store.RagSearch().execute({"query": "widget"}, c))
    assert "app_widget -[calls]-> app_greet" in res.result["graph_excerpt"]
    assert calls["n"] == 1
    res = run(rag_store.RagSearch().execute({"query": "widget"}, c))
    assert calls["n"] == 1                          # cached — no re-parse

    g2 = dict(_GRAPH)
    g2["edges"] = [{"source": "app_widget", "target": "use_main",
                    "relation": "imports", "confidence": "EXTRACTED"}]
    (runner.graph_root(projects, "u", "p1") / "graph.json").write_text(
        json.dumps(g2))
    res = run(rag_store.RagSearch().execute({"query": "widget"}, c))
    assert calls["n"] == 2                          # rebuild invalidated
    excerpt = res.result["graph_excerpt"]
    assert "app_widget -[imports]-> use_main" in excerpt
    assert "app_greet" not in excerpt
