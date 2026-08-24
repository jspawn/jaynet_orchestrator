# graphify — per-project graphs for JayNet

Maps one project's code and docs into a traversable graph, so the agent can
**query structure instead of reading whole files**: `graph.query`,
`graph.explain`, `graph.path` answer "what calls this?", "explain this node",
"how do these two connect?" against the map.

Two build passes:

- **Code** — mapped locally via tree-sitter AST (the `graphifyy` pip package,
  Apache-2.0). No LLM, nothing leaves the box.
- **Docs/PDFs** — a semantic pass through the LiteLLM alias configured at
  `plugins.graphify.model` (keep it a local alias if the docs are private).
  This is the expensive part; it is also what auto-rebuild spends.

Output lives at `<project>/graphify-out/` and is deleted with the project.
This is a **derived** map — rebuild anytime. Not to be confused with the
curated kg (`kg.*`), which is hand-grown knowledge: the graph is derived,
the kg is curated.

## Enable and use

1. Enable in **Admin → Plugins** (builtin plugins are disabled by default).
2. In a project chat, ask the agent to map the project — or call
   `graph.build` directly. First build is the consent gate for everything
   below; `graph.status` shows progress and staleness.
3. The agent then reaches for `graph.query` / `graph.explain` / `graph.path`
   on its own when a project has a graph (a prompt hint steers it).

## What it adds beyond the build

- **Wiki pages as nodes** (default on): one node per project-wiki page
  (`files/wiki/`) plus `references` edges from markdown links — wiki pages
  get communities and appear in report/viz. A `/charter` interview's pages
  land here automatically. Opt-out: `plugins.graphify.wiki_nodes: false`.
- **Auto-rebuild** (opt-in): `plugins.graphify.auto_rebuild: true` +
  `auto_rebuild_delay_s` (default 120). File changes — web edits AND the
  agent's own `fs.*` writes — re-arm a per-project debounce; the rebuild
  fires after a quiet window. Off by default.
- **Bridge to the kg**: `graph.seed_kg` seeds the graph into the curated kg
  as `<project>/<node>` entities + relations (provenance attrs,
  merge-on-reseed, confirmation-gated).
- **Bridge to RAG**: project-bound `rag.search` results carry a
  `graph_excerpt` — the 1-hop graph neighborhood around the hits — when a
  graph exists.

## Honesty note

The graph is a **current best-effort map, not ground truth**: AST extraction
is exact, but the doc semantic pass and INFERRED edges can be wrong or
stale. `graph.explain` cites the file/line a claim comes from — when it
matters, read the file. Staleness is tracked (edits flag the graph dirty)
and with auto-rebuild the map self-heals; without it, rebuild after bigger
changes.
