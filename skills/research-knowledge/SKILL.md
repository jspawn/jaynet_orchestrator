---
name: research-knowledge
description: Research and knowledge work — deep-research runs, web scraping, extraction and crawling, arxiv scholarship, RAG collections, and persistent memory. Load for research, scraping, ingesting, or remembering information.
---
# Research & Knowledge

**Trigger:** research, scrape, extract, crawl, arxiv, rag, knowledge graph, remember, ingest

## Deep research
* `research.start` — begin a budgeted, multi-source, cited investigation. Load the **deep-research** skill for the full workflow.
* `research.add` / `research.next` / `research.seen` / `research.note` / `research.report` — frontier-based research state machine.

## Web scraping
* `web.extract` — point at a page + describe the data → validated JSON file. Runs isolated so the page doesn't bloat context.
* `web.crawl` — same, but across PAGINATED pages. Follows next links / ?page= patterns up to a hard cap, merges into one JSON file.
* `web.render` — headless-browser text. ONLY when `web.fetch` returns thin/empty on JS-heavy pages.
* `browser.screenshot` / `browser.pdf` — capture a page's VISUAL.

## Scholarship
* `arxiv.search` / `arxiv.get` — ML/AI papers. Prefer over `web.search` for academic content.

## RAG (indexed document retrieval)
* `rag.index` — ingest documents into a collection with embeddings.
* `rag.search` — retrieve relevant chunks from indexed collections.
* `rag.collections` / `rag.delete` — manage collections.

## Knowledge graph
* `kg.upsert_entity` / `kg.add_relation` / `kg.remove_relation` — track relationships between files, models, concepts, or components.
* `kg.query` / `kg.neighbors` — explore the graph.

## Memory (long-term)
* `memory.search` / `memory.get` — (always available in core)
* `memory.append` — log durable preferences, tricky fixes, project structures. Search first to avoid duplicates.
* `memory.list` / `memory.delete` — manage entries.

## Verification
* `verify.score` — continuous [0,1] quality score via LLM judge. Gate non-code deliverables on a threshold.
* `verify.rank` — score and pick the best of N candidates.
* `verify.probe` — debug the verifier's raw first-token logprobs.
* `trace.query` / `trace.mine` — read past runs to debug failures or mine recurring patterns.
