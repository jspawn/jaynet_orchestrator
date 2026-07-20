---
name: selftest
description: Run a self-test of the whole toolset — call every available tool once with the smallest safe input and report what works. Load when the user asks to test, check, verify, or smoke-test the tools/the orchestrator, or asks "do all the tools work".
---
# Toolset self-test

Goal: call every tool available to you at least once with the smallest, safest input,
and report what works. You already know your full tool list — cover all of it.

Default to **safe mode** (read-only, or create-then-clean-up, never touching real data).
Switch to **full mode** only if the user explicitly asks to test cost/side-effect tools
(cloud LLM calls, model serving, job spawning).

## Plan the ORDER before you call anything (this is the important part)

Most self-test failures are not real bugs — they're **ordering** bugs: a tool is called
before the thing it depends on exists. `fs.read` before any file was written, `deliver.files`
on a missing file, `verify.score` before the verifier is up. Avoid this by planning in three
tiers and running them in order. Do NOT batch a consumer together with the tool that produces
what it consumes.

**Tier 1 — Independent probes (safe to batch).** Tools that need no fixture and no running
service: `gpu.status`, `ops.status`, `serve.list`, `skill.list`, `git.status`/
`git.log`, `web.search`, `web.fetch`, `arxiv.search`, `trace.query`. Fire these together first —
they also tell you what's healthy (use `ops.status` to learn which servers are up before Tier 3).
For `web.fetch`, use `https://example.com` — a stable, always-up page. Do NOT invent test URLs
from memory (httpbin.org and friends are flaky or dead). If the box has no internet route,
fetch a local page instead (e.g. `http://127.0.0.1:8071/` — this web UI's login page) and mark
it **ok (local only)**.

**Tier 2 — Producer → consumer chains (STRICT order within each chain).** Run the producer,
**capture the real path/id/collection it returns from the result**, then pass THAT exact value
to the consumer. Never assume a path — use what the producer handed back (this also avoids the
workspace-root path confusion). The chains:

- **file:** `fs.write` a tiny file → then `fs.read` / `fs.edit` / `fs.find` / `code.symbols` /
  `code.tree` / `code.patch` / `lint.run` / `deliver.files` / `pdf.create` on **that returned path**.
- **rag:** `rag.index` one short string into a `selftest` collection → `rag.search` it →
  `rag.collections` → `rag.delete` the collection.
- **memory:** `memory.append` a `selftest`-tagged note → `memory.search` for it → `memory.delete`
  that one only.
- **kg:** `kg.add_relation` (`selftest_a`→`selftest_b`) → query/`kg.neighbors` → remove it.
- **job** *(full mode):* `job.start` a 1-second `sleep 1` → capture the `job_id` →
  `job.status`/`job.logs`/`job.wait` → `job.cancel` — all using that id.

Each producer is one turn; run it, read its result, THEN do the consumers. Consumers of the
same producer can be batched together (they only depend on the producer, not each other).

**Tier 3 — Service-dependent checks (verify the service is up FIRST).** These need a running
model/server, so gate each on the Tier-1 `ops.status`/`serve.list` result:

- **verify:** run `verify.probe` first — if it reports a grade at a dominant position, the verifier
  is reachable; only then run `verify.score`/`verify.rank`. If the probe says the verifier is down,
  mark score/rank **skipped (verifier not serving)**, not failed.
- **council:** needs its panel models up — check `ops.status` shows brain + a specialist; otherwise
  skip with that note.
- **[cost] llm:** *(full mode)* one 1-token prompt like "ping".
- **[side-effect] serve/model:** `serve.list`/`serve.health` are always safe. Only in full mode,
  `serve.start` a small embedding model (or `model.use` a preset) → confirm health → `serve.stop`.
  A slot with no free VRAM is an environment state (skip), not a bug.

## Rules

- Work only under your writable roots. Put every artifact in one throwaway folder
  `selftest-<random>/` inside the **scratch dir you were given** (the `/tmp/orchrun-…` path
  from the "Your workspace" section — it is auto-deleted when the run ends, so nothing
  pollutes the project files). Only if no scratch dir was given (bare CLI), use the
  workspace root and delete the folder when done. Never modify anything that existed
  before this run.
- **Create before you query.** Every fixture must exist before a consumer points at it —
  the first `fs.write` into `selftest-<random>/` creates the folder for you (parent dirs
  are made automatically). Never hand `fs.find` / `fs.list` / `fs.read` a path you only
  guessed: a "no such file or directory … list it to see exact names" error there means
  YOU skipped the producer step, not that the tool is broken. Fix the order and retry.
- One clean demonstration per tool. On failure, record the EXACT error text and move on; retry
  at most once with a corrected input, then mark it failed. Don't rabbit-hole.
- **Distinguish "ordering/prereq" and "service down" from real bugs.** If a call fails only
  because a prerequisite wasn't ready, that's a planning miss on your part — fix the order and
  rerun it, don't report it as a broken tool. If it fails because a server isn't running (rag
  embedder, a model slot, Playwright/Chromium not installed), mark it **skipped — environment**,
  with the exact error.
- Iteration-heavy: if the budget is tight, either ask the user to raise the per-run iteration
  budget, or fan out — spawn one sub-agent per namespace (each does its own Tier 1→3 for that
  namespace) and aggregate. Keep a producer and its consumers inside the SAME sub-agent, or the
  consumer won't see the fixture.

## Report

When finished: delete the selftest folder if it lives in the workspace root (the scratch dir
cleans itself), then output ONE markdown table grouped by namespace —
columns: **Tool | Result (ok / failed / skipped) | Note**. Put the exact error text for every
failure. End with a one-line summary (N ok, M failed, K skipped) and explicitly separate real
bugs from "ordering fixed on retry" and "environment / server not running" results.
