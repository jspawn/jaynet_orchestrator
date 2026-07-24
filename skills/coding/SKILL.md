---
name: coding
description: >
  Write, build, fix, debug, refactor, test, or lint code — the tight inner loop:
  navigate → change → verify → checkpoint, with heavy lifting delegated to the
  specialist GPU (code.delegate) and complex builds planned first (architect /
  coding-projects). Load for any coding task. Complements coding-projects
  (multi-unit planning across runs) and codebase-review (understanding or
  auditing a repo you don't know).
---
# Coding — delegate, then navigate, change, verify, checkpoint

This is the per-edit choreography. For breaking a large build into units that fit
the budget, load `coding-projects`; this skill is what you do *within* a unit.
The discipline throughout: act on `path:line` handles, read only the spans you
need, never paste whole files or full command output back into context.

## 0. Delegate the heavy lifting (when a specialist model is configured)
If this is a self-contained, multi-step change and a dedicated specialist is set up,
prefer `code.delegate task="…"` — it runs the work on a sub-agent backed by the
stronger specialist model and keeps the bulky file/diff/test transcript out of your
context entirely. Give it a complete standalone task (repo path, the change, the
done-check) and pass `verify` (a test/lint command) so it's gated on a real
check, not its own say-so. Do single-line edits yourself; delegate one unit at
a time on a big build, not the whole project at once. For a COMPLEX task
(multi-file build, non-trivial refactor, ambiguous design) when the complexity
gate fires, hand it to `architect` — it plans, has the specialist poke holes, and
executes in a fresh context.

## 1. Orient (cheap, once per unit)
- `code.tree <dir>` for a one-shot structural map instead of many `fs.list` calls.
- `code.symbols <name> mode=definitions` to jump to where something is defined;
  `mode=references` to see who uses it. Both return `path:line` + the one line —
  then `fs.read` that file with a tight `start_line`/`end_line`.
- Use `fs.grep` for free-text/string hunts; use `code.symbols` for identifiers.

## 2. Change
- Small, exact, single-spot edit → `fs.edit` (unique-string replace).
- Larger or multi-hunk/multi-file change → build a unified diff and apply it with
  `code.patch` (run with `dry_run=true` first to confirm it applies cleanly).
- New file → `fs.write`.

## 3. Verify — cheapest signal first
1. `lint.run <path>` — fast static check (ruff/mypy/format/etc.). Fix obvious
   issues (optionally `fix=true` for formatters) before spending more.
2. `code.run "<cmd>"` — run the actual command synchronously and read the exit
   code + output tail: a focused test (`pytest tests/x.py::test_y`), a build, a
   script. This is the workhorse; it's confined + sandboxed (no network) so it's
   safe to call freely.
3. `test.run` — for the in-process ASGI/mock test harness when that's the right
   shape (it's confirmation-gated and heavier; don't reach for it for a quick check).
- Missing imports? `code.deps action=install packages=[...]` into the project venv,
  then re-run.
- Long / GPU / detached work (training, big builds) does NOT belong in `code.run`
  — hand it to `job.start` and poll with `job.status` / `job.logs`.
- For pure computation/parsing/small plots (math, JSON, regex, a quick chart),
  `code.execute` is the sandboxed scratchpad (no network, no project venv; files
  you want to keep go to the `ORCH_EXEC_OUT` dir and come back as written_files).

## 4. Checkpoint
- `git.status` → `git.diff` (review) → `git.add` → `git.commit`. Commit small,
  working units so a bad step is easy to unwind.
- Park half-done work before switching context with `git.stash`; discard a bad
  edit with `git.restore <paths>` (destructive — stash first if unsure).
- Sync with the remote (e.g. the NAS) via `git.fetch` / `git.pull` (`ff_only` by
  default) and publish with `git.push` (use `set_upstream=true` for a new branch).
- Working in parallel or spawning a sub-agent to build something risky? Give it an
  isolated checkout with `git.worktree add path=<repo>.worktrees/<name> branch=<name>
  create_branch=true`, point the sub-agent's cwd there, and `git.worktree remove`
  it when merged or abandoned — so concurrent work never stomps on the main tree.

## 4a. When something breaks
If a tool failed earlier in this run (or a previous one) and you're not sure why,
`trace.query view=failures` shows recent failures, and `view=events run_id=...`
replays a specific run's calls. Use it to recover instead of guessing.

## 5. Done check
A unit is done when `lint.run` passes and the unit's command (`code.run`) or test
(`test.run`) exits 0, and the change is committed. Report the result compactly —
the exit code and a one-line summary, not the full logs.
