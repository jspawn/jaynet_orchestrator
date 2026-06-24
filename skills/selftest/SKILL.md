---
name: selftest
description: Run a self-test of the whole toolset — call every available tool once with the smallest safe input and report what works. Load when the user asks to test, check, verify, or smoke-test the tools/the orchestrator, or asks "do all the tools work".
---
# Toolset self-test

Goal: call every tool available to you at least once with the smallest, safest possible
input, and report what works. You already know your full tool list — go through all of it,
namespace by namespace.

Default to **safe mode** (read-only, or create-then-clean-up, never touching real data).
Switch to **full mode** only if the user explicitly asks to test cost/side-effect tools
(cloud LLM calls, model serving, job spawning).

## Rules

- Work only under your writable workspace. Put every test artifact in one throwaway folder
  like `data/work/selftest-<random>/` and delete that folder when you're done. Never modify,
  delete, or overwrite anything that existed before this run — no pre-existing files,
  memories, knowledge-graph relations, RAG collections, git history, or running servers.
- Batch independent read-only checks into a single turn to conserve iterations.
- Each tool needs exactly one clean demonstration. If a call fails, record the EXACT error
  text and move on; retry at most once with a corrected input, then mark it failed. Don't
  rabbit-hole on any single tool.
- When a tool needs a prerequisite, create it first with an earlier tool (write a file
  before reading/delivering it; index a doc before searching it; start a short job before
  checking/cancelling it).
- In safe mode, skip anything tagged [cost] or [side-effect] below and mark it skipped.
  In full mode, exercise those too, as cheaply as possible.
- This is iteration-heavy. If the budget looks tight, either ask the user to raise the
  per-run iteration budget (the Budget field in Run options), or fan out: spawn one
  sub-agent per namespace and aggregate their reports.

## Safe check per namespace

Adapt to your actual tool names and parameters.

- **fs** — write a tiny file in the selftest dir, read it back, list the dir, edit it.
- **code** — `code.execute` a one-liner that prints `2+2`. `code.run` `echo ok` in the
  selftest dir (safe; but if the sandbox is disabled in config it's confirmation-gated —
  skip rather than approve). `code.symbols` for a symbol in the file you wrote;
  `code.tree` the selftest dir; `code.patch` with `dry_run=true` on a one-line diff (never
  apply). FULL MODE only: [side-effect] `code.deps` (installs into a venv, needs network)
  and [cost][side-effect] `code.delegate` (spawns a sub-agent on the coder model).
- **lint** — `lint.run` on the file you wrote (read-only). If no linters are installed it
  returns `passed: null` with a `missing` list — that's an environment state, a pass, not a bug.
- **trace** — `trace.query view=runs` then `view=failures` (both read-only; never write).
- **web** — `web.search` for "example"; `web.fetch` `https://example.com`. Then `web.render`
  the same URL — if Playwright or the Chromium binary isn't installed it returns an
  actionable "not installed / run playwright install" error; treat that as an environment
  state (headless browser not provisioned), not a code bug, exactly like rag below.
- **arxiv** — search "transformer", 1 result.
- **gpu** — read GPU status.
- **skill** — `skill.list`, then `skill.load` EACH listed skill once to confirm every skill
  (including local-coding) loads cleanly. Read-only.
- **mcp** — list connected MCP servers/tools (empty is a pass).
- **memory** — write a memory tagged "selftest", search for it, then delete that one only.
- **kg** — add a relation (`selftest_a` → `selftest_b`), query it, then remove that relation.
- **rag** — index one short string into a `selftest` collection, search it, list collections,
  then delete the `selftest` collection. Needs the embedding server; if it's down, mark rag
  failed with the exact error — that's an environment state, not a code bug.
- **git** — read-only only in safe mode: `git.status` / `git.diff` / `git.log` / `git.show`.
  Everything that writes — `add`/`commit`/`branch`, and [side-effect] `fetch`/`pull`/`push`/
  `stash`/`restore`/`worktree` — is confirmation-gated: skip it in safe mode and NEVER run it
  against a real repo. In full mode, exercise the mutating ones only inside a throwaway repo
  you `git init` in the selftest dir.
- **deliver** — deliver the tiny file you created so it appears as a download.
- **agent** — spawn a sub-agent whose entire task is to reply "subagent ok" using no tools.
- **eval / test** — run the most trivial harmless check you can; if it would touch real code
  or data, skip and mark skipped.
- **[cost] llm** — call a cloud model with a 1-token prompt like "ping" (full mode only).
- **[side-effect] serve** — `serve.list` / `serve.status` / `serve.health` are always safe;
  only in full mode, `serve.start` a small embedding model and then `serve.stop` it.
- **[side-effect] job** — full mode only: start a 1-second job (e.g. `sleep 1`), then
  list / status / cancel it.

## Report

When finished: delete the selftest folder, then output ONE markdown table grouped by
namespace — columns: **Tool | Result (ok / failed / skipped) | Note**. Put the exact error
text for every failure. End with a one-line summary (N ok, M failed, K skipped) and
explicitly separate anything that looks like a real bug from expected "server not running /
not configured" results.
