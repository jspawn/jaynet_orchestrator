---
name: pageindex
description: Answer questions over LONG PDFs (reports, contracts, manuals — 100+ pages, or a document you'll question repeatedly) via a persistent tree index — vectorless retrieval, all local. doc.index builds the index once, doc.tree shows section titles/summaries/page ranges, doc.pages reads exact pages. Load when the user wants to "search this document", asks "what does the contract say about …", or works with a long PDF. For SHORT documents doc.extract is enough — don't index those.
---

# Tree-indexed document QA (pageindex plugin)

The tools are `doc.index`, `doc.tree` and `doc.pages`. They are NOT in the
default toolset — if missing from your tools, call `tools.load` with
`namespaces=["doc"]` first (this also brings `doc.extract`, which stays the
right tool for SHORT documents).

## Workflow

1. **Index once per document**: `doc.index` action=build path=<PDF in your
   workspace>. Indexing a long PDF takes MINUTES — a local model maps the
   whole document into a section tree — but the index is PERSISTENT: reuse
   the returned `doc_id` for every later question, never rebuild. Already
   indexed something? `doc.index` action=list shows the doc_ids.
2. **Navigate**: `doc.tree` doc_id=<id> returns the section tree — titles,
   summaries, page ranges, no page text. Find the nodes that could hold the
   answer; `node_summary=false` gives a bare titles-only skeleton for very
   large trees.
3. **Read**: `doc.pages` doc_id=<id> pages="12-14,27" reads the exact page
   text. Keep ranges narrow — a few pages per call; output is capped and
   huge dumps crowd your context.
4. **Answer with page citations** ("p. 27") so the user can verify against
   the original.

## What to know

- Everything runs locally: indexing goes through the JayNet LiteLLM proxy
  (the configured `plugins.pageindex.model` alias), the index store lives in
  `<data>/pageindex/`. Nothing leaves the box.
- Text-based PDFs only — no OCR. Scanned documents stay with `doc.extract` /
  the `pdf` skill.
- The index pays off on LONG documents and repeat questions only. For a
  short document or a one-shot read, `doc.extract` (or the `long-document`
  skill for a too-big text) is cheaper and just as good.
- Retrieval quality depends on the local model that builds the tree — if a
  section you expected is missing, check neighbouring page ranges with
  doc.pages; page numbers in the tree are reliable anchors.
