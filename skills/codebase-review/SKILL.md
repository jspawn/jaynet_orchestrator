---
name: codebase-review
description: Explore, review, audit, or modify a code repository safely. Load when asked to review, audit, refactor, debug across, or understand a codebase.
---
# Working across a codebase

A playbook for repo-scale work using your existing tools. Don't read every file —
navigate deliberately.

## 1. Orient before reading

- `fs.list` the root and key dirs; read `README`, `pyproject.toml`/`package.json`,
  and any config to learn structure, entry points, and how it's built/run.
- `git.log` and `git.status` for recent activity and working-tree state.
- `fs.grep` for the symbols/strings central to the task (definitions, call sites,
  error messages) instead of opening files blind.

## 2. Read only what matters

Open the specific files the task touches. For a big sub-area you don't need in your
own context, delegate: `agent.spawn(task="Read X and Y and report how Z works in
≤200 words", tools=["fs.read","fs.grep"])` — the child's file reads stay out of
your thread.

## 3. Change safely

- Make focused edits with `fs.edit`/`fs.write`. Keep diffs small and explain each.
- **Verify with tests**: `test.run` (quick, in-process) after changes; reproduce a
  bug as a failing test first, then fix until green. Don't declare done untested.
- Stage/commit with `git.add` / `git.commit` only when asked; show the `git.diff`
  first. These are confirmation-gated — surface what you're about to commit.

## 4. Report

Summarise findings and changes concretely (files, functions, line-level intent),
list what you verified vs. assumed, and call out risks or follow-ups. For an audit
(no changes), deliver prioritised findings with file:line references.
