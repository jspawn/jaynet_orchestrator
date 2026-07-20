---
name: codebase-review
description: UNDERSTAND, review, or audit a repository you don't already know — orient, read only what matters (delegating big sub-areas), and report findings with file:line references. Load to explore/audit/understand a codebase or answer "how does X work here". For making the changes themselves follow coding; for a multi-unit build use coding-projects.
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

## 3. Change safely (only if the task calls for edits)

For the actual edit → verify → commit loop, follow **coding** (`code.symbols`
to locate, `fs.edit`/`code.patch` to change, `lint.run` then `code.run`/`test.run`
to verify, `git` to checkpoint). The review-specific points that still apply:

- Keep diffs small and explain each; reproduce a bug as a failing test before fixing.
- Show the `git.diff` before you stage/commit — those steps are confirmation-gated.
- Don't declare done untested.

## 4. Report

Summarise findings and changes concretely (files, functions, line-level intent),
list what you verified vs. assumed, and call out risks or follow-ups. For an audit
(no changes), deliver prioritised findings with file:line references.
