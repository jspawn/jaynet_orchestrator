"""rag.index must be confined to the run's work roots, exactly like fs.* —
otherwise it can chunk/embed arbitrary files (~/.ssh/id_rsa) into the RAG
store, where rag.search would exfiltrate them."""
import pytest

from conftest import run
from tools.rag import store as rag_store
from tools.rag.store import RagIndex


@pytest.fixture
def rag_ctx(ctx, project, tmp_path, monkeypatch):
    # Hermetic: stub the embedder, point the DB at a throwaway file.
    async def fake_embed(texts, _ctx):
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(rag_store, "_embed", fake_embed)
    cfg = {"tools": {"rag": {"db_path": str(tmp_path / "rag.db")},
                     "fs": {"allowed_roots": [str(project)]}}}
    return ctx(config=cfg, work_root=str(project))


def test_rag_index_path_inside_root_ok(project, rag_ctx):
    (project / "notes.txt").write_text("hello rag world")
    r = run(RagIndex().execute({"collection": "c", "path": "notes.txt"}, rag_ctx))
    assert r.status == "ok" and r.result["chunks_indexed"] == 1


def test_rag_index_rejects_absolute_path_outside_root(rag_ctx):
    r = run(RagIndex().execute({"collection": "c", "path": "/etc/hostname"}, rag_ctx))
    assert r.status == "error" and "workspace" in r.error


def test_rag_index_rejects_home_secret_escape(rag_ctx):
    # The original bug: ~/.ssh/id_rsa expanded and indexed without confinement.
    r = run(RagIndex().execute({"collection": "c", "path": "~/.ssh/id_rsa"}, rag_ctx))
    assert r.status == "error" and "workspace" in r.error


def test_rag_index_missing_file_inside_root_is_error(project, rag_ctx):
    r = run(RagIndex().execute({"collection": "c", "path": "nope.txt"}, rag_ctx))
    assert r.status == "error" and "nope.txt" in r.error


def test_rag_index_dedup_skips_embed_and_storage(ctx, project, tmp_path, monkeypatch):
    """Re-indexing identical text must neither re-embed (CPU server) nor
    double-store. The hash column is added by migration, so a fresh DB also
    exercises the ALTER path."""
    calls = []

    async def fake_embed(texts, _ctx):
        calls.append(len(texts))
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(rag_store, "_embed", fake_embed)
    cfg = {"tools": {"rag": {"db_path": str(tmp_path / "rag.db")},
                     "fs": {"allowed_roots": [str(project)]}}}
    c = ctx(config=cfg, work_root=str(project))

    (project / "notes.txt").write_text("hello rag world")
    r1 = run(RagIndex().execute({"collection": "c", "path": "notes.txt"}, c))
    assert r1.status == "ok" and r1.result["chunks_indexed"] == 1
    assert calls == [1]

    r2 = run(RagIndex().execute({"collection": "c", "path": "notes.txt"}, c))
    assert r2.status == "ok"
    assert r2.result["chunks_indexed"] == 0
    assert r2.result["skipped_duplicates"] == 1
    assert calls == [1]                                # embedder never re-called

    import sqlite3
    n = sqlite3.connect(str(tmp_path / "rag.db")).execute(
        "SELECT COUNT(*) FROM rag_doc WHERE collection='c'").fetchone()[0]
    assert n == 1                                      # no duplicate rows

    # Same text into a DIFFERENT collection is not a duplicate.
    r3 = run(RagIndex().execute({"collection": "other", "path": "notes.txt"}, c))
    assert r3.status == "ok" and r3.result["chunks_indexed"] == 1
