---
name: coding
description: Write, build, fix, debug, refactor, test, or lint code — delegate non-trivial tasks to the dedicated coder GPU, run sandboxed snippets, and patch files surgically. Load for any coding task.
---
# Coding Tools

**Trigger:** code, build, fix, debug, refactor, test, lint, implement, delegate, architect

## Tools

### Delegation
* `code.delegate` — your DEFAULT for non-trivial coding. Runs on the dedicated coder GPU (Tess-4-27B / Qwen3.6). Pass a COMPLETE, standalone task. Pass `verify` (a test/lint command) to gate on a real check. The coder is ~3-4× slower but reasons harder per token.
* `architect` — plan-first handler for COMPLEX tasks. Plans, has the coder poke holes, arbitrates, writes a handoff, executes. Use when complexity gate fires.

### Code execution
* `code.execute` — sandboxed Python scratchpad. Math, JSON, regex, parsing large outputs. **Isolated: no network, no project venv, no GPU.**
* `code.run` — shell command in an **isolated sandbox — no network, no project venv, no GPU**. For self-contained computation, NOT for hitting live services.
* `code.tree` / `code.symbols` — orient and locate in a codebase before reading.
* `code.patch` — apply a unified diff atomically. Prefer over many `fs.edit`s.
* `code.deps` — manage project venv dependencies.

### Testing & linting
* `test.run` — pytest in the **project venv** with project importable and **network ON**. Use for project tests AND (via `command` override) to reach local services.
* `lint.run` — ruff/mypy/formatters. Run before declaring code done.

## Workflow tips
* Verify before declaring code done (test + lint).
* Large/multi-file: write `PLAN.md` first, complete one unit per run.
* Keep context lean: `code.symbols` + `fs.grep` to locate, read by range, prefer `fs.edit`/`code.patch`.
* Load **coding-projects** skill for the full multi-run workflow.
