---
name: project-graph
description: Use the current project's knowledge graph (graph.* tools) to answer architecture and "what connects X to Y" questions by traversing the graph instead of reading whole files. Use when a project has a graph (the project prefix says so) or the user asks to map a project.
---

# Project knowledge graph

When a JayNet project has a knowledge graph (the `[Knowledge graph]` line in
the project prefix), it maps every concept in the project's files — code
symbols, doc topics — and how they connect. Query it BEFORE reading files.

## Workflow

1. **No graph yet** (no hint line): offer `graph.build`. It runs in the
   background — call it, then poll `graph.status` until `state` is `ready`.
2. **Architecture / relationship questions**: `graph.query "<question>"` for a
   scoped subgraph, `graph.explain "<symbol>"` for one concept and all its
   connections, `graph.path A B` to trace how two things connect.
3. **Then read code** only for the few files the graph points at — not to
   explore.

## Reading edges

Every edge is tagged `EXTRACTED` (explicitly in the source — trust it) or
`INFERRED` (resolved by graphify — verify before relying on it). Tell the
user which kind a claim rests on when it matters.

## Staleness

The graph is a snapshot. If `graph.status` shows `dirty: true` (files changed
since the build) or answers contradict what you see in files, run
`graph.build` to refresh, and prefer the files where they disagree.
