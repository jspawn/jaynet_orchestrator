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

import hashlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np

from runtime.tool_base import (
    Tool,
    ToolContext,
    ToolResult,
    resolve_in_roots,
    work_roots,
)


def _cfg(ctx: ToolContext) -> dict:
    return ctx.config.get("tools", {}).get("rag", {})


def _db(ctx: ToolContext) -> sqlite3.Connection:
    from runtime.paths import RAG_DB
    path = _cfg(ctx).get("db_path", str(RAG_DB))
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
    # Content-hash dedup for rag.index (re-indexing identical text must not
    # re-embed or double-store). Migrate DBs that predate the column — NULL
    # hashes simply never match and get re-embedded once.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rag_doc)")}
    if "hash" not in cols:
        conn.execute("ALTER TABLE rag_doc ADD COLUMN hash TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_hash ON rag_doc(collection, hash)")
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunk(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    step = max(1, size - overlap)
    return [text[i:i + size] for i in range(0, len(text), step)]


async def _embed(texts: list[str], ctx: ToolContext) -> list[list[float]]:
    """Call an OpenAI-compatible /v1/embeddings endpoint. Mockable in tests."""
    cfg = _cfg(ctx)
    from runtime.paths import LITELLM_BASE
    base = ctx.config.get("orchestrator", {}).get("litellm_base", LITELLM_BASE)
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
            # Confine exactly like the fs.* tools: only files under this run's
            # work roots may be indexed. Without this, rag.index could chunk and
            # embed ~/.ssh/id_rsa into the store, where rag.search would serve it.
            try:
                p = resolve_in_roots(work_roots(ctx), args["path"])
            except (PermissionError, FileNotFoundError) as e:
                return ToolResult(status="error", result=None, error=str(e))
            text = p.read_text(encoding="utf-8", errors="replace")
            source = source or str(p)
        if not text:
            return ToolResult(status="error", result=None, error="provide text or path")

        chunks = _chunk(text, int(cfg.get("chunk_size", 1200)), int(cfg.get("chunk_overlap", 150)))
        if not chunks:
            return ToolResult(status="error", result=None, error="nothing to index")

        # Dedup by content hash BEFORE embedding: re-indexing identical text
        # must not burn CPU embed calls or store duplicate rows. (Rows from
        # before the hash column have NULL and simply never match.)
        hashes = [hashlib.sha256(c.encode("utf-8")).hexdigest() for c in chunks]
        conn = _db(ctx)
        try:
            existing = {r[0] for r in conn.execute(
                "SELECT hash FROM rag_doc WHERE collection = ? AND hash IS NOT NULL",
                (args["collection"],))}
            seen: set[str] = set()
            keep: list[int] = []
            for i, h in enumerate(hashes):
                if h in existing or h in seen:
                    continue
                seen.add(h)
                keep.append(i)
            skipped = len(chunks) - len(keep)
            if not keep:
                return ToolResult(status="ok", result={
                    "collection": args["collection"], "chunks_indexed": 0,
                    "skipped_duplicates": skipped, "source": source,
                    "note": "all chunks already present in this collection"})
            try:
                vecs = await _embed([chunks[i] for i in keep], ctx)
            except Exception as e:
                return ToolResult(status="error", result=None, error=f"embed failed: {e}")

            ts = _now()
            for i, vec in zip(keep, vecs):
                arr = np.asarray(vec, dtype=np.float32)
                conn.execute(
                    "INSERT INTO rag_doc(collection, source, chunk_idx, text, dim, embedding, ts, hash)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (args["collection"], source, i, chunks[i], arr.shape[0],
                     arr.tobytes(), ts, hashes[i]),
                )
            conn.commit()
            return ToolResult(status="ok", result={
                "collection": args["collection"], "chunks_indexed": len(keep),
                "skipped_duplicates": skipped,
                "source": source, "dim": int(np.asarray(vecs[0]).shape[0])})
        finally:
            conn.close()


class RagSearch(Tool):
    name = "rag.search"
    description = ("Retrieve the most relevant chunks for a query from a collection "
                  "(or all collections). Returns text + similarity score + source. "
                  "Set rerank=true to re-order with the configured reranker.")
    private = True
    read_only = True
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
        # Check the collection FIRST — if there's nothing to search, say so without
        # ever calling the embedder (a missing collection must not 400 on embed).
        conn = _db(ctx)
        try:
            if args.get("collection"):
                rows = conn.execute(
                    "SELECT id, collection, source, text, dim, embedding FROM rag_doc "
                    "WHERE collection = ?", (args["collection"],)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, collection, source, text, dim, embedding FROM rag_doc").fetchall()
            if not rows:
                avail = [r[0] for r in conn.execute(
                    "SELECT DISTINCT collection FROM rag_doc ORDER BY collection").fetchall()]
        finally:
            conn.close()

        if not rows:
            if not avail:
                note = ("no collections have been indexed yet — nothing to search. "
                        "Use rag.index first, or rely on the conversation/web instead.")
            elif args.get("collection"):
                note = (f"collection '{args['collection']}' not found. "
                        f"Available collections: {', '.join(avail)}.")
            else:
                note = "no matching documents."
            return ToolResult(status="ok", result={"matches": [], "count": 0, "note": note})

        # Only now do we need the embedder.
        try:
            qvec = (await _embed([args["query"]], ctx))[0]
        except Exception as e:
            return ToolResult(status="error", result=None, error=(
                f"the embedding server call failed ({e}). The RAG embedder may not be "
                f"running — start one with serve.start(kind='embedding', wire_rag=true), "
                f"or check tools.rag.embed_url. (rag.* needs an embedding server.)"))
        q = np.asarray(qvec, dtype=np.float32)
        qn = q / (np.linalg.norm(q) + 1e-8)

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
    read_only = True
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
