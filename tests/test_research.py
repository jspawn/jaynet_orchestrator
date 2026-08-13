"""Tests for the research.* state spine (frontier, dedup, budgets, claims, report)."""
from conftest import run

from runtime.tool_base import ToolContext
from tools.research.loop import (
    ResearchAdd,
    ResearchNext,
    ResearchNote,
    ResearchReport,
    ResearchSeen,
    ResearchStart,
    _source_score,
)


def ctx(tmp_path, **over):
    cfg = {"tools": {"research": {"db_path": str(tmp_path / "research.db"), **over}}}
    return ToolContext(request_id="t", config=cfg, budget=None)


def _start(c, **kw):
    r = run(ResearchStart().execute({"topic": "T", "questions": ["q1", "q2"], **kw}, c))
    assert r.status == "ok"
    return r.result["run_id"], r.result["collection"]


def test_start_seeds_frontier_and_collection(tmp_path):
    c = ctx(tmp_path)
    rid, coll = _start(c)
    assert coll == f"research_{rid}" and rid


def test_next_pops_and_charges_budget(tmp_path):
    c = ctx(tmp_path)
    rid, _ = _start(c, max_searches=2)
    r1 = run(ResearchNext().execute({"run_id": rid, "n": 1}, c))
    assert r1.result["stop"] is False and len(r1.result["questions"]) == 1
    r2 = run(ResearchNext().execute({"run_id": rid, "n": 1}, c))
    assert r2.result["stop"] is False
    # budget (2) now spent
    r3 = run(ResearchNext().execute({"run_id": rid}, c))
    assert r3.result["stop"] is True and "budget" in r3.result["reason"]


def test_frontier_empty_stops(tmp_path):
    c = ctx(tmp_path)
    rid, _ = _start(c, max_searches=50)
    run(ResearchNext().execute({"run_id": rid, "n": 8}, c))   # drains the 2 seeds
    r = run(ResearchNext().execute({"run_id": rid}, c))
    assert r.result["stop"] is True and r.result["reason"] == "frontier empty"


def test_seen_url_dedup(tmp_path):
    c = ctx(tmp_path)
    rid, _ = _start(c)
    r1 = run(ResearchSeen().execute({"run_id": rid, "urls": ["a", "b"]}, c))
    assert set(r1.result["new_urls"]) == {"a", "b"}
    r2 = run(ResearchSeen().execute({"run_id": rid, "urls": ["b", "c"]}, c))
    assert r2.result["new_urls"] == ["c"] and r2.result["duplicate_urls"] == ["b"]


def test_seen_content_dedup_exact(tmp_path):
    c = ctx(tmp_path, semantic_dedup=False)   # hash-only path
    rid, _ = _start(c)
    a = run(ResearchSeen().execute({"run_id": rid, "content": "hello world", "url": "u1"}, c))
    assert a.result["content_novel"] is True
    b = run(ResearchSeen().execute({"run_id": rid, "content": "hello   world", "url": "u2"}, c))
    assert b.result["content_novel"] is False   # whitespace-normalized hash matches


# Deterministic stub embedder: vector keys off topical words, so paraphrases of
# the same topic point the same way and unrelated topics are orthogonal.
async def _stub_embed(texts, _ctx):
    out = []
    for t in texts:
        tl = t.lower()
        out.append([1.0 if "cat" in tl else 0.0,
                    1.0 if "mat" in tl else 0.0,
                    1.0 if "quantum" in tl else 0.0])
    return out


def test_seen_semantic_dedup(tmp_path, monkeypatch):
    import tools.research.loop as R
    monkeypatch.setattr(R, "_embed", _stub_embed)
    c = ctx(tmp_path, semantic_dedup=True, dedup_threshold=0.92)
    rid, _ = _start(c)
    a = run(ResearchSeen().execute({"run_id": rid, "content": "the cat sat on the mat", "url": "u1"}, c))
    assert a.result["content_novel"] is True and a.result["dedup_method"] == "novel"
    # different wording + different URL -> different hash, but same meaning vector
    b = run(ResearchSeen().execute({"run_id": rid, "content": "a cat is resting on a mat", "url": "u2"}, c))
    assert b.result["content_novel"] is False and b.result["dedup_method"] == "semantic-duplicate"
    assert b.result["max_similarity"] >= 0.92
    # unrelated topic -> novel
    d = run(ResearchSeen().execute({"run_id": rid, "content": "quantum entanglement intro", "url": "u3"}, c))
    assert d.result["content_novel"] is True and d.result["dedup_method"] == "novel"


def test_seen_semantic_dedup_degrades_without_embedder(tmp_path, monkeypatch):
    import tools.research.loop as R
    async def boom(texts, _ctx):
        raise RuntimeError("embed server down")
    monkeypatch.setattr(R, "_embed", boom)
    c = ctx(tmp_path, semantic_dedup=True)
    rid, _ = _start(c)
    r = run(ResearchSeen().execute({"run_id": rid, "content": "anything", "url": "u1"}, c))
    assert r.result["content_novel"] is True            # hash-only fallback, no crash
    assert "hash-only" in r.result["dedup_method"]


def test_novelty_stall_stops(tmp_path):
    c = ctx(tmp_path, novelty_stall=2)
    rid, _ = _start(c, max_searches=50)
    # two cycles that add nothing new -> stall
    run(ResearchSeen().execute({"run_id": rid, "urls": ["x"]}, c))           # novel -> reset
    run(ResearchSeen().execute({"run_id": rid, "urls": ["x"]}, c))           # dup -> +1
    run(ResearchSeen().execute({"run_id": rid, "urls": ["x"]}, c))           # dup -> +1 (=2)
    r = run(ResearchNext().execute({"run_id": rid}, c))
    assert r.result["stop"] is True and "novelty" in r.result["reason"]


def test_add_respects_depth_and_dedup(tmp_path):
    c = ctx(tmp_path)
    rid, _ = _start(c, max_depth=1)
    # depth0 -> add at depth1 (ok)
    r = run(ResearchAdd().execute({"run_id": rid, "questions": ["q3", "q1"], "parent_depth": 0}, c))
    assert r.result["added"] == 1 and r.result["skipped_dup"] == 1   # q1 already seeded
    # depth1 -> would be depth2 > max_depth(1): dropped
    r2 = run(ResearchAdd().execute({"run_id": rid, "questions": ["q4"], "parent_depth": 1}, c))
    assert r2.result["added"] == 0 and r2.result["skipped_depth"] == 1


def test_note_scores_sources_and_closes_question(tmp_path):
    c = ctx(tmp_path)
    rid, _ = _start(c)
    nxt = run(ResearchNext().execute({"run_id": rid}, c))
    qid = nxt.result["questions"][0]["question_id"]
    r = run(ResearchNote().execute({"run_id": rid, "source": "https://www.reuters.com/x",
                                    "claims": ["c1", "c2"], "question_id": qid}, c))
    assert r.result["stored"] == 2 and r.result["source_score"] == 0.7


def test_source_scoring_tiers():
    cfg = {"deny": ["evil.test"]}
    assert _source_score("https://nih.gov/a", cfg) == 0.9          # .gov-ish high
    assert _source_score("https://reddit.com/r/x", cfg) == 0.3     # forum low
    assert _source_score("https://random-blog.io/p", cfg) == 0.5   # unknown
    assert _source_score("https://evil.test/p", cfg) == 0.0        # denied


def test_report_ranks_sources_and_groups_claims(tmp_path):
    c = ctx(tmp_path)
    rid, coll = _start(c)
    nxt = run(ResearchNext().execute({"run_id": rid, "n": 2}, c))
    q0, q1 = nxt.result["questions"]
    run(ResearchNote().execute({"run_id": rid, "source": "https://arxiv.org/abs/1",
                                "claims": ["high-quality claim"], "question_id": q0["question_id"]}, c))
    run(ResearchNote().execute({"run_id": rid, "source": "https://reddit.com/x",
                                "claims": ["low-quality claim"], "question_id": q1["question_id"]}, c))
    rep = run(ResearchReport().execute({"run_id": rid}, c))
    assert rep.status == "ok"
    assert rep.result["collection"] == coll
    assert rep.result["stats"]["distinct_sources"] == 2
    # arxiv (0.9) ranks above reddit (0.3)
    assert rep.result["ranked_sources"][0]["source"].startswith("https://arxiv.org")
    assert rep.result["ranked_sources"][0]["quality"] == 0.9
    # claims grouped by their sub-question
    assert len(rep.result["claims_by_question"]) == 2
