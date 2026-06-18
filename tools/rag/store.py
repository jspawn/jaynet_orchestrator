"""RAG tools — local retrieval over your own documents (the read side of the
'world model').

Storage is SQLite (one more DB alongside trace/memory/kg) with embeddings kept
as float32 blobs; search is brute-force cosine in numpy. That's plenty for a
single-user corpus up to tens of thousands of chunks and keeps the stack
consistent and serverless. For a much larger corpus, swap the search backend
for qdrant/hnsw later — the tool interface stays the same.

Embeddings come from any OpenAI-compatible /v1/embeddings endpoint
(tools.rag.embed_url + embed_model) — point it at your Qwen3-Embedding server or
let it default to the LiteLLM proxy. An optional reranker (tools.rag.rerank_url,
e.g. your bge-reranker) re-orders candidates when configured.

Marked private. NOTE: the embedding/rerank HTTP calls could not be exercised in
the build sandbox; the chunking, storage and cosine ranking were tested with a
deterministic stub embedder.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

from runtime.tool_base import Tool, ToolContext, ToolResult


def _cfg(ctx: ToolContext) -> dict:
    return ctx.config.get("tools", {}).get("rag", {})


def _db(ctx: ToolContext) -> sqlite3.Connection:
    path = _cfg(ctx).get("db_path", "/srv/orchestrator/data/rag.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rag_doc(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            source TEXT DEFAULT '',
            chunk_idx INTEGER DEFAULT 0,
            text TEXT NOT NULL,
            dim INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            ts TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_coll ON rag_doc(collection)")
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunk(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    step = max(1, size - overlap)
    return [text[i:i + size] for i in range(0, len(text), step)]


async def _embed(texts: list[str], ctx: ToolContext) -> list[list[float]]:
    """Call an OpenAI-compatible /v1/embeddings endpoint. Mockable in tests."""
    cfg = _cfg(ctx)
    base = ctx.config.get("orchestrator", {}).get("litellm_base", "http://127.0.0.1:4000")
    url = cfg.get("embed_url") or f"{base}/v1/embeddings"
    model = cfg.get("embed_model", "embedding")
    headers = {"Authorization": "Bearer " + os.environ.get("LITELLM_MASTER_KEY", "")}
    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=120) as client:
        for i in range(0, len(texts), 64):
            batch = texts[i:i + 64]
            r = await client.post(url, json={"model": model, "input": batch}, headers=headers)
            r.raise_for_status()
            data = r.json()
            out.extend(item["embedding"] for item in data["data"])
    return out


async def _rerank(query: str, docs: list[str], ctx: ToolContext) -> list[int] | None:
    """Optional rerank via a configured endpoint. Returns doc order (indices) or None."""
    cfg = _cfg(ctx)
    url = cfg.get("rerank_url")
    if not url:
        return None
    model = cfg.get("rerank_model", "reranker")
    headers = {"Authorization": "Bearer " + os.environ.get("LITELLM_MASTER_KEY", "")}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json={"model": model, "query": query, "documents": docs},
                              headers=headers)
        r.raise_for_status()
        data = r.json()
    # Accept Cohere/Jina-style {"results":[{"index":i,"relevance_score":s},...]}
    results = data.get("results") or data.get("data")
    if not results:
        return None
    results = sorted(results, key=lambda x: x.get("relevance_score", x.get("score", 0)),
                     reverse=True)
    return [int(x["index"]) for x in results]


class RagIndex(Tool):
    name = "rag.index"
    description = ("Embed and store text into a named collection for later retrieval. "
                  "Provide raw `text` or a `path` to a text file. Chunks long input "
                  "automatically.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "collection": {"type": "string", "description": "Collection name to add to."},
            "text": {"type": "string", "description": "Raw text to index."},
            "path": {"type": "string", "description": "Path to a text file to index."},
            "source": {"type": "string", "description": "Label for provenance (url/file/note)."},
        },
        "required": ["collection"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        cfg = _cfg(ctx)
        text = args.get("text")
        source = args.get("source", "")
        if args.get("path"):
            p = Path(args["path"]).expanduser()
            if not p.exists():
                return ToolResult(status="error", result=None, error=f"no such file: {p}")
            text = p.read_text(encoding="utf-8", errors="replace")
            source = source or str(p)
        if not text:
            return ToolResult(status="error", result=None, error="provide text or path")

        chunks = _chunk(text, int(cfg.get("chunk_size", 1200)), int(cfg.get("chunk_overlap", 150)))
        if not chunks:
            return ToolResult(status="error", result=None, error="nothing to index")
        try:
            vecs = await _embed(chunks, ctx)
        except Exception as e:
            return ToolResult(status="error", result=None, error=f"embed failed: {e}")

        conn = _db(ctx)
        try:
            ts = _now()
            for idx, (chunk, vec) in enumerate(zip(chunks, vecs)):
                arr = np.asarray(vec, dtype=np.float32)
                conn.execute(
                    "INSERT INTO rag_doc(collection, source, chunk_idx, text, dim, embedding, ts)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (args["collection"], source, idx, chunk, arr.shape[0],
                     arr.tobytes(), ts),
                )
            conn.commit()
            return ToolResult(status="ok", result={
                "collection": args["collection"], "chunks_indexed": len(chunks),
                "source": source, "dim": int(np.asarray(vecs[0]).shape[0])})
        finally:
            conn.close()


class RagSearch(Tool):
    name = "rag.search"
    description = ("Retrieve the most relevant chunks for a query from a collection "
                  "(or all collections). Returns text + similarity score + source. "
                  "Set rerank=true to re-order with the configured reranker.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "collection": {"type": "string", "description": "Restrict to this collection. "
                           "Omit to search all."},
            "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 30},
            "rerank": {"type": "boolean", "default": False},
        },
        "required": ["query"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            qvec = (await _embed([args["query"]], ctx))[0]
        except Exception as e:
            return ToolResult(status="error", result=None, error=f"embed failed: {e}")
        q = np.asarray(qvec, dtype=np.float32)
        qn = q / (np.linalg.norm(q) + 1e-8)

        conn = _db(ctx)
        try:
            if args.get("collection"):
                rows = conn.execute(
                    "SELECT id, collection, source, text, dim, embedding FROM rag_doc "
                    "WHERE collection = ?", (args["collection"],)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, collection, source, text, dim, embedding FROM rag_doc").fetchall()
        finally:
            conn.close()

        if not rows:
            return ToolResult(status="ok", result={"matches": [], "count": 0,
                                                    "note": "collection empty or not found"})

        mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        norms = np.linalg.norm(mat, axis=1, keepdims=True) + 1e-8
        sims = (mat / norms) @ qn
        top_k = min(int(args.get("top_k", 5)), len(rows))
        # over-fetch when reranking so the reranker has candidates to work with
        fetch = min(len(rows), top_k * 4 if args.get("rerank") else top_k)
        order = np.argsort(-sims)[:fetch]

        cand = [{"id": int(rows[i]["id"]), "collection": rows[i]["collection"],
                 "source": rows[i]["source"], "score": round(float(sims[i]), 4),
                 "text": rows[i]["text"]} for i in order]

        if args.get("rerank"):
            try:
                new_order = await _rerank(args["query"], [c["text"] for c in cand], ctx)
                if new_order:
                    cand = [cand[i] for i in new_order if i < len(cand)]
            except Exception:
                pass  # reranker optional; fall back to cosine order
        cand = cand[:top_k]
        for c in cand:
            if len(c["text"]) > 800:
                c["text"] = c["text"][:800] + "…"
        return ToolResult(status="ok", result={"query": args["query"],
                                                "count": len(cand), "matches": cand})


class RagCollections(Tool):
    name = "rag.collections"
    description = "List indexed collections with chunk counts."
    private = True
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _db(ctx)
        try:
            rows = conn.execute(
                "SELECT collection, COUNT(*) AS chunks, COUNT(DISTINCT source) AS sources "
                "FROM rag_doc GROUP BY collection ORDER BY collection").fetchall()
            return ToolResult(status="ok", result={"collections": [dict(r) for r in rows]})
        finally:
            conn.close()


class RagDelete(Tool):
    name = "rag.delete"
    description = "Delete an entire collection (all its chunks)."
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {"collection": {"type": "string"}},
        "required": ["collection"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _db(ctx)
        try:
            cur = conn.execute("DELETE FROM rag_doc WHERE collection = ?", (args["collection"],))
            conn.commit()
            return ToolResult(status="ok", result={"collection": args["collection"],
                                                    "deleted_chunks": cur.rowcount})
        finally:
            conn.close()
