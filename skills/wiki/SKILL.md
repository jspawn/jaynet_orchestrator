---
name: wiki
description: >
  Maintain the user's persistent LLM wiki: a directory of interlinked markdown
  pages (index.md + log.md + topic pages) that compiles knowledge once and
  keeps it current, instead of re-deriving it per question. Load when the user
  invokes /llmwiki or asks to view, add, update, remove, or organize
  information in their wiki.
---

# Wiki — a persistent, LLM-maintained knowledge base

The wiki is a plain directory of markdown files. You (the agent) write and
maintain all of it; the user reads it and directs you. Knowledge is compiled
once and kept current — never re-derived from scratch per question.

The run that loaded this skill tells you the wiki's absolute path. All paths
below are relative to it. Read/write with your normal `fs.*` tools.

## What goes where (division of labor)

- **Wiki** — compiled, interlinked knowledge meant to outlive a chat: topic
  pages, entity pages, comparisons, decisions with rationale, syntheses.
- **memory.*** — small facts about the user and their preferences. Not wiki
  material.
- **RAG** — raw source documents. The wiki holds the *synthesis*; cite the
  source, don't copy it.

## Layout

- `index.md` — catalog of every page: link + one-line summary, grouped by
  category (topics, entities, sources, comparisons). Update on EVERY change.
- `log.md` — append-only chronology. Each entry starts with
  `## [YYYY-MM-DD] <op> | <page-or-topic>` so it stays grep-able.
- Everything else — one markdown file per page, kebab-case names
  (`sbb-supersaver-tickets.md`), optional subdirectories per category.

## Page conventions

- Start with `# Title`, then the content; keep pages focused (one topic).
- Link related pages with relative markdown links (`see [X](x.md)`).
- Note sources and dates for claims that can go stale (`(as of 2026-08)`,
  source URL). When new info contradicts a page, update the page and say what
  changed in the log — don't leave both versions.

## Workflows

**View / answer a question.** Read `index.md` first, drill into the listed
pages, answer from them. If the answer required real synthesis, ask the user
whether to file it as a new page — good answers should compound, not vanish
into chat.

**Add / update.** Create or revise the page(s), update `index.md`, add the
cross-links both ways, append a log entry. One focused addition touches a
handful of pages, not fifteen — stay proportional.

**Remove.** Delete the page, remove it from `index.md`, fix any inbound links
on other pages, append a log entry. Confirm with the user before deleting
anything beyond a trivial stub.

**Ingest a source.** Read it, extract what's durable, integrate: update the
affected pages (or create one), flag contradictions with existing claims,
update `index.md`, log it. Discuss the key takeaways with the user before
writing if the source is large.

**Lint (on request).** Health-check: contradictions between pages, stale
claims, orphan pages nothing links to, concepts mentioned but lacking a page,
missing cross-references. Report findings; fix only what the user approves.

## Rules

- You own the wiki's prose; the user owns the decisions. Ask before large
  restructures or deletions.
- Every mutation ends with: `index.md` updated + `log.md` entry appended.
- Keep it plain markdown — no frontmatter requirement, no tooling beyond fs.*.
