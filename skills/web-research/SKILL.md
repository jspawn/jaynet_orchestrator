---
name: web-research
description: Answer a question from a HANDFUL of web sources with a quick, sourced synthesis — plan, gather, cross-check, cite. Load for open-ended or current-information questions that need a few sources or a comparison. For an exhaustive, budgeted investigation with dedup, per-source provenance and a full cited report, use deep-research instead.
---
# Multi-source web research

A playbook for going beyond a single lookup. Goal: a synthesised, sourced answer —
not a pile of links.

## 1. Plan

Break the question into 2–5 sub-questions or facets. Note what would *change your
answer* so you know when you've found enough.

## 2. Gather

- `web.search` each facet separately (distinct queries beat one broad query).
- `web.fetch` the few most promising results for full text — snippets are thin.
- Prefer primary/authoritative sources (official docs, papers, filings, vendor
  pages) over aggregators; note the date for anything time-sensitive.

For a broad sweep, fan out with sub-agents so the raw pages stay out of your
context: `agent.spawn(task="Research <facet>; return 5 bullet findings with source
URLs", tools=["web.search","web.fetch"])`, then combine their briefs.

**When a page comes back as just a title or empty (JS-heavy single-page apps, gov
portals, dashboards):** don't re-fetch it with `web.fetch` — the content isn't in the
HTML. Two pivots, in order of preference:
1. **Find the data/API behind it** (best — cheaper and structured). For geoportals
   and map sites look for **WFS/WMS/OGC** services, "OGD"/open-data registries, or a
   documented REST/JSON API (`web.search "<site> WFS GetCapabilities"`,
   `"<org> open data API"`). Hit `GetCapabilities` first to learn the real layer names
   and parameters before requesting features, and read the server's own error text
   (it usually names the permitted `OUTPUTFORMAT`/version) instead of guessing.
2. **`web.render`** — if there's no usable API and you just need the rendered text,
   fetch it through the headless browser. Slower; use it as the fallback, not the
   first move.

## 3. Cross-check and synthesise

- Corroborate key claims across ≥2 independent sources; flag disagreement rather
  than averaging it away.
- Write the answer in your own words, organised by the question's facets, with
  source URLs attached to the claims they support.

## 4. Be honest about limits

Distinguish well-supported from thin/single-source claims, note anything you
couldn't verify, and give the date for fast-moving topics. If the question needs
data behind a login or paywall you can't reach, say so.

If the findings are worth reusing later in the conversation, consider
`rag.index`-ing them so you can `rag.search` without re-fetching.
