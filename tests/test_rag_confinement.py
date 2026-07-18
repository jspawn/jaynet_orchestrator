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
