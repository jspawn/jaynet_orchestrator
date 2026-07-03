---
name: deep-research
description: >
  Run deep, iterative web research on a topic: plan sub-questions, crawl with
  dedup into a temporary RAG collection, distil follow-up questions, optionally
  fan out to sub-agents, rank by source quality, and produce a cited summary.
  Load when the user asks to "research", "deep dive", "investigate thoroughly",
  "find everything about", or wants a sourced report rather than a quick answer.
  For a handful of sources and a quick synthesis, use web-research instead.
---
# Deep research — plan, crawl, distil, rank, cite

A disciplined loop, not a one-shot fan-out. The `research.*` tools hold the state
(frontier of open questions, visited/dedup set, budgets, claims+provenance); you
drive the crawling with web.search / web.fetch / rag.index / rag.search and,
when it helps, agent.spawn. The whole point is to stay signal-dense and to STOP
when you stop learning.

## 1. Plan first (cheapest lever on quality)
Decompose the topic into 3–6 concrete sub-questions. If the topic is broad or the
stakes are high, show them to the user and let them adjust before crawling.
Then open the run:
`research.start topic="…" questions=[…] max_depth=2 max_searches=24`
Keep the returned `run_id` and `collection`.

## 2. The loop
Repeat until `research.next` returns `stop:true`:

1. `research.next run_id n=…` — take 1–N open sub-questions (N = your fan-out
   width, ≤ max_subagents). Respect the stop signal immediately.
2. For each question, `web.search` it (1–2 focused queries).
3. `research.seen run_id urls=[…]` on the result URLs — **fetch only the
   `new_urls`**. This is what keeps you from re-crawling and flooding the RAG.
4. Triage by the snippet + source quality: fetch full text only for results that
   clear a relevance/quality bar. Don't fetch "every result" — that's where the
   compute disappears.
5. For each kept page: `web.fetch` → `research.seen run_id content=<text> url=<url>`
   (skip if `content_novel:false`) → `rag.index collection=<coll> text=<text>
   source=<url>` → extract a handful of atomic factual claims and
   `research.note run_id source=<url> claims=[…] question_id=<id>`.
6. Distil 1–3 sharper follow-up questions from what you just learned and push them
   with `research.add run_id questions=[…] parent_depth=<depth>` (it enforces
   max_depth and dedups).

**Fanning out:** for independent sub-questions, `agent.spawn` a sub-agent per
question (not per URL — that saturates everything), giving each the same run_id,
collection, and the web/rag/research tools. Each writes into the shared RAG and
calls research.note; the heavy fetched text stays in the sub-agent's context, not
yours. Cap concurrency at max_subagents.

## 3. Stop conditions (don't override them)
`research.next` stops the run when the search budget is spent, the frontier is
empty, or novelty has stalled (recent crawls added nothing new — the dedup gate
feeds this). Trust it; chasing past a novelty stall just burns budget.

## 4. Synthesize with citations
- `research.report run_id` — sources ranked by quality (with claim counts),
  claims grouped by sub-question with provenance, and any unexplored questions.
- `rag.search collection=<coll> query=… rerank=true` for the semantic detail on
  each sub-question.
- Write the summary from both: lead with the answer, attribute claims to sources,
  **prefer higher-quality sources and explicitly flag where sources disagree**
  (contradictions are a finding, not noise). Note what remained unexplored if the
  run stopped on budget.

## 5. Lifecycle
The `research_<run_id>` collection is retained so the user can ask follow-ups
against the corpus without re-crawling (`rag.search` it directly). When they're
done, `rag.delete collection=<coll>` to reclaim space.

## Guardrails
Never crawl or cite sources that promote hate, violence, or other harmful content;
skip them even if a search returns them. Respect the budgets — deep research is the
easiest way to accidentally spend a whole run's tokens.
