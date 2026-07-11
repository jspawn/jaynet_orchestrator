"""research.* — the state spine for deep, iterative web research.

The actual crawling is done by the orchestrator with tools it already has
(web.search, web.fetch, rag.index/search, agent.spawn) — see the `deep-research`
skill. What was missing is the *durable state* that turns a one-shot fan-out into
a disciplined loop that knows when to stop and where every claim came from:

  • a FRONTIER queue of open sub-questions with depth + priority,
  • a VISITED set (URL + content hash + per-page embedding) so parallel sub-agents
    don't re-crawl or flood the RAG with duplicates — including pages that repeat
    the same facts in different words (caught by embedding similarity, not just a
    hash) — the single biggest waste in naive deep search,
  • hard BUDGETS (depth / search-steps) plus a novelty-stall gate ("are we still
    learning anything?") so a run terminates instead of fanning out forever,
  • CLAIMS with per-source provenance and a heuristic source-quality score, so the
    final summary can cite, weight good sources over SEO noise, and surface
    cross-source agreement/contradiction.

State lives in its own SQLite DB (data/research.db). Each run gets a RAG
collection `research_<run_id>` (retained after the run so you can ask follow-ups
without re-crawling; delete with rag.delete when done). Verbs:

  research.start  — open a run, seed the frontier, get the collection name
  research.next   — pop the next sub-question(s) to work on, or a stop signal
  research.seen   — dedup gate: which URLs / content are new (skip the rest)
  research.add    — push newly-discovered sub-questions onto the frontier
  research.note   — record claims with their source (auto source-scored)
  research.report — assemble the ranked, sourced material for final synthesis

All private; none mutate anything outside the research DB + its RAG collection.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from runtime.tool_base import Tool, ToolContext, ToolResult


async def _embed(texts: list[str], ctx: ToolContext) -> list[list[float]]:
    """Embed via the shared RAG embedding endpoint (one embedder for the stack).
    Defined here as a thin wrapper so tests can monkeypatch it."""
    from tools.rag.store import _embed as _rag_embed
    return await _rag_embed(texts, ctx)


# ----------------------------------------------------------------------------- helpers
def _cfg(ctx: ToolContext) -> dict:
    return ctx.config.get("tools", {}).get("research", {}) or {}


def _db(ctx: ToolContext) -> sqlite3.Connection:
    from runtime.paths import RESEARCH_DB
    path = _cfg(ctx).get("db_path", str(RESEARCH_DB))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS research_run(
        run_id TEXT PRIMARY KEY, topic TEXT, status TEXT, created REAL,
        collection TEXT, max_depth INTEGER, max_searches INTEGER, max_subagents INTEGER,
        searches_used INTEGER DEFAULT 0, consec_dups INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS research_q(
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, question TEXT,
        depth INTEGER, priority REAL, status TEXT DEFAULT 'open');
      CREATE TABLE IF NOT EXISTS research_seen(
        run_id TEXT, url TEXT, content_hash TEXT);
      CREATE TABLE IF NOT EXISTS research_claim(
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, question_id INTEGER,
        claim TEXT, source_url TEXT, source_score REAL, ts REAL);
      CREATE TABLE IF NOT EXISTS research_emb(
        run_id TEXT, url TEXT, dim INTEGER, embedding BLOB);
      CREATE INDEX IF NOT EXISTS idx_q_run ON research_q(run_id, status);
      CREATE INDEX IF NOT EXISTS idx_seen_run ON research_seen(run_id, url);
      CREATE INDEX IF NOT EXISTS idx_claim_run ON research_claim(run_id);
      CREATE INDEX IF NOT EXISTS idx_emb_run ON research_emb(run_id);
    """)
    conn.commit()
    return conn


def _run(conn, run_id):
    return conn.execute("SELECT * FROM research_run WHERE run_id=?", (run_id,)).fetchone()


# Heuristic source-quality score in [0,1]. Config can extend each tier; the host
# match is suffix-based so subdomains inherit (docs.python.org -> python.org tier).
_HIGH = (".gov", ".edu", ".ac.uk", ".mil", "doi.org", "arxiv.org", "ncbi.nlm.nih.gov",
         "pubmed.gov", "who.int", "nature.com", "science.org", "ietf.org", "w3.org",
         "europa.eu", "admin.ch", "iso.org")
_MED = ("reuters.com", "apnews.com", "bbc.co.uk", "bbc.com", "economist.com",
        "springer.com", "sciencedirect.com", "acm.org", "ieee.org", "wikipedia.org")
_LOW = ("reddit.com", "quora.com", "medium.com", "pinterest.com", "facebook.com",
        "x.com", "twitter.com", "tiktok.com", "blogspot.com", "wordpress.com")


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _source_score(url: str, cfg: dict) -> float:
    host = _host(url)
    if not host:
        return 0.4
    def match(host, tiers): return any(host == t or host.endswith(t) for t in tiers)
    deny = tuple(cfg.get("deny") or ())
    if deny and match(host, deny):
        return 0.0
    if match(host, tuple(cfg.get("high") or ()) + _HIGH):
        return 0.9
    if match(host, tuple(cfg.get("medium") or ()) + _MED):
        return 0.7
    if match(host, tuple(cfg.get("low") or ()) + _LOW):
        return 0.3
    return 0.5  # unknown


# ----------------------------------------------------------------------------- start
class ResearchStart(Tool):
    name = "research.start"
    description = (
        "Open a deep-research run: seed the frontier with sub-questions and get back "
        "a run_id + a RAG collection name to index findings into. Decompose the topic "
        "into 3–6 concrete sub-questions FIRST and pass them as `questions` — that "
        "planning step is the biggest lever on quality."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "The overall research topic/goal."},
            "questions": {"type": "array", "items": {"type": "string"},
                          "description": "Seed sub-questions (depth 0). Falls back to the topic."},
            "max_depth": {"type": "integer", "default": 2, "minimum": 1, "maximum": 4,
                          "description": "How many levels of follow-up questions to chase."},
            "max_searches": {"type": "integer", "default": 24, "minimum": 1, "maximum": 200,
                             "description": "Hard cap on search-steps (questions explored)."},
            "max_subagents": {"type": "integer", "default": 4, "minimum": 0, "maximum": 12,
                              "description": "Advisory cap the skill uses when fanning out."},
        },
        "required": ["topic"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        topic = (args.get("topic") or "").strip()
        if not topic:
            return ToolResult(status="error", result=None, tool_name=self.name, error="topic required")
        rid = uuid.uuid4().hex[:10]
        coll = f"research_{rid}"
        qs = [q.strip() for q in (args.get("questions") or []) if q.strip()] or [topic]
        conn = _db(ctx)
        try:
            conn.execute(
                "INSERT INTO research_run(run_id, topic, status, created, collection, "
                "max_depth, max_searches, max_subagents) VALUES (?,?,?,?,?,?,?,?)",
                (rid, topic, "active", time.time(), coll,
                 int(args.get("max_depth", 2)), int(args.get("max_searches", 24)),
                 int(args.get("max_subagents", 4))))
            for i, q in enumerate(qs):
                conn.execute("INSERT INTO research_q(run_id, question, depth, priority) "
                             "VALUES (?,?,?,?)", (rid, q, 0, 1.0 - i * 0.01))
            conn.commit()
        finally:
            conn.close()
        return ToolResult(status="ok", result={
            "run_id": rid, "collection": coll, "seeded": len(qs),
            "budgets": {"max_depth": int(args.get("max_depth", 2)),
                        "max_searches": int(args.get("max_searches", 24)),
                        "max_subagents": int(args.get("max_subagents", 4))},
            "hint": "index every kept page with rag.index(collection, text, source=<url>)",
        }, tool_name=self.name)


# ----------------------------------------------------------------------------- next
class ResearchNext(Tool):
    name = "research.next"
    description = (
        "Pop the next open sub-question(s) to work on, highest-priority/shallowest "
        "first, and charge them against the search budget. Returns {stop:true,reason} "
        "when the run should end — budget spent, frontier empty, or novelty stalled "
        "(recent crawls added nothing new). Honor the stop signal."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "n": {"type": "integer", "default": 1, "minimum": 1, "maximum": 8,
                  "description": "How many sub-questions to take this cycle (fan-out width)."},
        },
        "required": ["run_id"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _db(ctx)
        try:
            run = _run(conn, args["run_id"])
            if not run:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error="unknown run_id")
            stall = int(_cfg(ctx).get("novelty_stall", 3))
            if run["searches_used"] >= run["max_searches"]:
                return ToolResult(status="ok", result={"stop": True,
                    "reason": f"search budget reached ({run['max_searches']})"}, tool_name=self.name)
            if run["consec_dups"] >= stall:
                return ToolResult(status="ok", result={"stop": True,
                    "reason": f"novelty stalled ({stall} cycles without new sources)"},
                    tool_name=self.name)
            remaining = run["max_searches"] - run["searches_used"]
            take = min(int(args.get("n", 1)), remaining)
            rows = conn.execute(
                "SELECT id, question, depth FROM research_q WHERE run_id=? AND status='open' "
                "ORDER BY depth ASC, priority DESC, id ASC LIMIT ?", (args["run_id"], take)
            ).fetchall()
            if not rows:
                return ToolResult(status="ok", result={"stop": True, "reason": "frontier empty"},
                                  tool_name=self.name)
            ids = [r["id"] for r in rows]
            conn.executemany("UPDATE research_q SET status='active' WHERE id=?",
                             [(i,) for i in ids])
            conn.execute("UPDATE research_run SET searches_used=searches_used+? WHERE run_id=?",
                         (len(rows), args["run_id"]))
            conn.commit()
            return ToolResult(status="ok", result={
                "stop": False,
                "questions": [{"question_id": r["id"], "question": r["question"],
                               "depth": r["depth"]} for r in rows],
                "budget_left": remaining - len(rows),
            }, tool_name=self.name)
        finally:
            conn.close()


# ----------------------------------------------------------------------------- seen (dedup)
class ResearchSeen(Tool):
    name = "research.seen"
    description = (
        "Dedup gate. BEFORE fetching, pass candidate `urls` to learn which are new "
        "(fetch only those). AFTER fetching, pass the page text as `content`: it's "
        "caught as a duplicate if it matches a kept page exactly (hash) OR is "
        "semantically near-identical to one already kept (embedding similarity), so "
        "the same facts in different words don't flood the RAG. Updates the novelty "
        "counter that drives the stop signal."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "urls": {"type": "array", "items": {"type": "string"},
                     "description": "Candidate URLs to check + mark."},
            "content": {"type": "string",
                        "description": "Page text to hash-check for near-duplicate content."},
            "url": {"type": "string", "description": "URL the content belongs to (with content)."},
        },
        "required": ["run_id"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _db(ctx)
        try:
            run = _run(conn, args["run_id"])
            if not run:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error="unknown run_id")
            out = {}
            any_novel = False

            if args.get("urls"):
                seen = {r[0] for r in conn.execute(
                    "SELECT url FROM research_seen WHERE run_id=?", (args["run_id"],)).fetchall()}
                new, dup = [], []
                for u in args["urls"]:
                    (dup if u in seen else new).append(u)
                for u in new:
                    conn.execute("INSERT INTO research_seen(run_id, url) VALUES (?,?)",
                                 (args["run_id"], u))
                    seen.add(u)
                out["new_urls"] = new
                out["duplicate_urls"] = dup
                any_novel = any_novel or bool(new)

            if args.get("content"):
                norm = " ".join(args["content"].split())
                h = hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()
                exact = conn.execute(
                    "SELECT 1 FROM research_seen WHERE run_id=? AND content_hash=? LIMIT 1",
                    (args["run_id"], h)).fetchone()
                novel = not exact
                method = "exact-duplicate" if exact else None
                sim = None
                rcfg = _cfg(ctx)
                # Semantic dedup: a page that says the same thing as one we already
                # kept, in different words, won't share a hash — so embed it and
                # compare against the embeddings of pages kept so far this run.
                if novel and rcfg.get("semantic_dedup", True):
                    prefix = int(rcfg.get("dedup_prefix_chars", 2000))
                    try:
                        vec = (await _embed([norm[:prefix]], ctx))[0]
                        v = np.asarray(vec, dtype=np.float32)
                        v = v / (np.linalg.norm(v) + 1e-8)
                        kept = conn.execute("SELECT embedding FROM research_emb WHERE run_id=?",
                                            (args["run_id"],)).fetchall()
                        if kept:
                            mat = np.stack([np.frombuffer(r[0], dtype=np.float32) for r in kept])
                            mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-8)
                            sim = float(np.max(mat @ v))
                        thr = float(rcfg.get("dedup_threshold", 0.92))
                        if sim is not None and sim >= thr:
                            novel = False
                            method = "semantic-duplicate"
                        else:
                            method = "novel"
                            # keep this page's signature for future comparisons
                            conn.execute("INSERT INTO research_emb(run_id, url, dim, embedding) "
                                         "VALUES (?,?,?,?)", (args["run_id"], args.get("url", ""),
                                                              int(v.shape[0]), v.tobytes()))
                    except Exception:
                        # embedder unavailable -> degrade to hash-only dedup, don't crash
                        method = "hash-only (embedder unavailable)"
                conn.execute("INSERT INTO research_seen(run_id, url, content_hash) VALUES (?,?,?)",
                             (args["run_id"], args.get("url", ""), h))
                out["content_novel"] = novel
                if method:
                    out["dedup_method"] = method
                if sim is not None:
                    out["max_similarity"] = round(sim, 4)
                any_novel = any_novel or novel

            # Novelty gate: reset on any new material, else advance the stall counter.
            if any_novel:
                conn.execute("UPDATE research_run SET consec_dups=0 WHERE run_id=?", (args["run_id"],))
            else:
                conn.execute("UPDATE research_run SET consec_dups=consec_dups+1 WHERE run_id=?",
                             (args["run_id"],))
            conn.commit()
            out["novel"] = any_novel
            return ToolResult(status="ok", result=out, tool_name=self.name)
        finally:
            conn.close()


# ----------------------------------------------------------------------------- add (expand frontier)
class ResearchAdd(Tool):
    name = "research.add"
    description = (
        "Push newly-discovered sub-questions onto the frontier at the next depth "
        "level. Dedups against existing questions and drops any beyond max_depth — "
        "so distilling follow-ups can't loop forever."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "questions": {"type": "array", "items": {"type": "string"}},
            "parent_depth": {"type": "integer", "default": 0,
                             "description": "Depth of the question these came from."},
        },
        "required": ["run_id", "questions"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _db(ctx)
        try:
            run = _run(conn, args["run_id"])
            if not run:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error="unknown run_id")
            depth = int(args.get("parent_depth", 0)) + 1
            added = skip_depth = skip_dup = 0
            if depth > run["max_depth"]:
                return ToolResult(status="ok", result={
                    "added": 0, "skipped_depth": len(args.get("questions") or []),
                    "skipped_dup": 0, "note": "beyond max_depth"}, tool_name=self.name)
            existing = {r[0].strip().lower() for r in conn.execute(
                "SELECT question FROM research_q WHERE run_id=?", (args["run_id"],)).fetchall()}
            for q in (args.get("questions") or []):
                q = q.strip()
                if not q:
                    continue
                if q.lower() in existing:
                    skip_dup += 1; continue
                conn.execute("INSERT INTO research_q(run_id, question, depth, priority) "
                             "VALUES (?,?,?,?)", (args["run_id"], q, depth, 0.8))
                existing.add(q.lower()); added += 1
            conn.commit()
            return ToolResult(status="ok", result={
                "added": added, "skipped_dup": skip_dup, "skipped_depth": skip_depth,
                "depth": depth}, tool_name=self.name)
        finally:
            conn.close()


# ----------------------------------------------------------------------------- note (claims)
class ResearchNote(Tool):
    name = "research.note"
    description = (
        "Record atomic claims extracted from a source, with provenance. Each claim is "
        "auto-scored by source quality (primary/official > reputable > unknown > "
        "forum/SEO) so the final report can weight and cite them. Mark the "
        "question_id this source was answering when you can."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "source": {"type": "string", "description": "Source URL the claims came from."},
            "claims": {"type": "array", "items": {"type": "string"},
                       "description": "Short, self-contained factual statements."},
            "question_id": {"type": "integer", "description": "Frontier question this answers."},
        },
        "required": ["run_id", "source", "claims"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _db(ctx)
        try:
            if not _run(conn, args["run_id"]):
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error="unknown run_id")
            score = _source_score(args["source"], _cfg(ctx))
            qid = args.get("question_id")
            n = 0
            for c in (args.get("claims") or []):
                c = c.strip()
                if not c:
                    continue
                conn.execute("INSERT INTO research_claim(run_id, question_id, claim, "
                             "source_url, source_score, ts) VALUES (?,?,?,?,?,?)",
                             (args["run_id"], qid, c, args["source"], score, time.time()))
                n += 1
            # Mark the answered question done.
            if qid:
                conn.execute("UPDATE research_q SET status='done' WHERE id=? AND run_id=?",
                             (qid, args["run_id"]))
            conn.commit()
            return ToolResult(status="ok", result={"stored": n, "source_score": score},
                              tool_name=self.name)
        finally:
            conn.close()


# ----------------------------------------------------------------------------- report
class ResearchReport(Tool):
    name = "research.report"
    description = (
        "Assemble the run's material for final synthesis: sources ranked by quality "
        "(with claim counts), claims grouped by sub-question with provenance, and any "
        "unexplored frontier questions. Pair this with rag.search over the run's "
        "collection for the semantic detail, then write the cited summary."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "max_claims": {"type": "integer", "default": 60, "minimum": 5, "maximum": 300},
            "top_sources": {"type": "integer", "default": 15, "minimum": 1, "maximum": 60},
        },
        "required": ["run_id"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        conn = _db(ctx)
        try:
            run = _run(conn, args["run_id"])
            if not run:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error="unknown run_id")
            # Sources ranked by quality, then coverage.
            srcs = conn.execute(
                "SELECT source_url, source_score, COUNT(*) AS claims FROM research_claim "
                "WHERE run_id=? GROUP BY source_url ORDER BY source_score DESC, claims DESC "
                "LIMIT ?", (args["run_id"], int(args.get("top_sources", 15)))).fetchall()
            # Claims grouped by question (best-sourced first within each group).
            claim_rows = conn.execute(
                "SELECT c.question_id, q.question, c.claim, c.source_url, c.source_score "
                "FROM research_claim c LEFT JOIN research_q q ON q.id=c.question_id "
                "WHERE c.run_id=? ORDER BY c.source_score DESC, c.id ASC LIMIT ?",
                (args["run_id"], int(args.get("max_claims", 60)))).fetchall()
            groups: dict = {}
            for r in claim_rows:
                key = r["question"] or "(general)"
                groups.setdefault(key, []).append({
                    "claim": r["claim"], "source": r["source_url"],
                    "source_score": round(r["source_score"], 2)})
            unexplored = [r[0] for r in conn.execute(
                "SELECT question FROM research_q WHERE run_id=? AND status='open' "
                "ORDER BY depth, priority DESC", (args["run_id"],)).fetchall()]
            n_sources = conn.execute(
                "SELECT COUNT(DISTINCT source_url) FROM research_claim WHERE run_id=?",
                (args["run_id"],)).fetchone()[0]
            conn.execute("UPDATE research_run SET status='reported' WHERE run_id=?",
                         (args["run_id"],))
            conn.commit()
            return ToolResult(status="ok", result={
                "run_id": run["run_id"], "topic": run["topic"], "collection": run["collection"],
                "stats": {"searches_used": run["searches_used"], "max_searches": run["max_searches"],
                          "distinct_sources": n_sources,
                          "total_claims": conn.execute(
                              "SELECT COUNT(*) FROM research_claim WHERE run_id=?",
                              (args["run_id"],)).fetchone()[0]},
                "ranked_sources": [{"source": r["source_url"],
                                    "quality": round(r["source_score"], 2),
                                    "claims": r["claims"]} for r in srcs],
                "claims_by_question": groups,
                "unexplored_questions": unexplored,
                "note": ("synthesize from this + rag.search over the collection; cite sources, "
                         "prefer higher-quality ones, and call out where sources disagree. "
                         "Collection is retained — rag.delete it when finished."),
            }, tool_name=self.name)
        finally:
            conn.close()
