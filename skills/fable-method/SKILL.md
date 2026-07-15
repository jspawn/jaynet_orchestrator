# The Fable Method

A structured problem-solving loop: classify the ask, define done, gather evidence, decide, act surgically, verify by observation, report outcome-first. A mid-tier model that follows this loop beats a stronger model that free-styles.

**When to use:** any multi-step task that no task-specific skill covers, or when asked for "fable method", "approach this like Fable", or when you want disciplined execution on something that matters.

**Tools you'll need:** `fs.list`, `fs.find`, `fs.read`, `fs.grep` (orient), `code.run`/`test.run` (verify), `code.delegate` (act), `verify.score` (quality gate). Load via `skill.load` if not available.

The steps structure your work, never your output: do not narrate step numbers in anything the user reads.

---

## Triviality gate (run first)

A task is trivial ONLY if ALL true: one file, under ~10 changed lines, no new behavior, you already know exactly what to change without searching. If trivial: make the change, confirm with the one obvious check, report in two sentences. Everything else gets the full loop.

## Step 0 — Classify the ask

| Shape | Signal | Deliverable |
|---|---|---|
| **Question / assessment** | "why is…", "what do you think…" | Findings + one recommendation. Change nothing. |
| **Task** | "fix", "build", "change", "make" | The completed change, verified. |
| **Plan-first** | ambiguous scope, irreversible actions, or user asks for a plan | A plan with your recommendation. Stop and wait. |

Tie-breaks: plan-first signal present → plan-first wins. Mixed ask ("why is this failing, fix it") → task whose report also answers the question. Genuinely unsure → plan-first.

"Ambiguous scope" test: you can imagine two materially different deliverables. If evidence gathering (Step 2) can settle it, proceed. If only the user can, ask exactly one pointed question stating your recommended interpretation.

## Step 1 — Define done

One or two sentences: what done looks like and how it will be verified.

- **Task:** a concrete observation (this test passes, build stays green, this file exists).
- **Question:** every claim traces to something you actually read or ran — cite file+line or command output.
- **Plan-first:** a plan the user can approve, with verification named per step.

If you cannot name a verification, ask the user one specific clarifying question before proceeding.

## Step 2 — Gather evidence

1. **Orient first.** `fs.list .` / `fs.find` before reading anything specific. You cannot pick files from memory.
2. **Primary sources beat memory.** Read actual code, files, output. Never invent an API signature or file path from recall. Use `web.search`/`web.fetch` for library docs.
3. **Parallelize independent lookups.** Web fetches, doc lookups, reads across many files → one batch.
4. **Read narrow, never re-read.** `fs.grep` to locate, then read that section. Don't re-fetch what's in context.
5. **Time-box.** One round of lookups + one follow-up. Third round needs a stated reason. Two consecutive lookups with nothing new → stop.
6. **Establish intent before changing behavior.** A failing check has two culprits: code or the check itself. Find the statement of intended behavior (README, spec, docstring). If code, check, and spec disagree, that's a surprise (rule 7) — surface the contradiction, never silently make one side match another.
7. **Surprises route the loop.** Anything contradicting your expectation is your most important finding. State it. If it changes what done means → update Step 1. If it changes the ask → back to Step 0.

## Step 3 — Decide and commit

Synthesize evidence into ONE recommendation. If you seriously considered alternatives, name each in one line and say why it lost.

Route by Step 0 table. For tasks, proceed to Step 4 without asking permission. An action is irreversible if another person or system can observe it before you could undo it (push, deploy, send, delete shared data). Local working tree edits are reversible.

## Step 4 — Act surgically

1. **Intent gate.** Before any behavior-changing edit, write: `INTENT: code does <X>; the failing check expects <Y>; the spec says <Z>`. You must actually open the spec to fill slot Z. If X, Y, Z disagree → do not edit yet, the disagreement is the finding. Authority order: explicit user statement > spec > tests > current code.
2. **Smallest correct change.** Touch only what the task needs. Match existing style.
3. **Precise edits over rewrites.** Rewrite only if you authored it this session or fully read it.
4. **Track multi-part work.** 3+ heterogeneous steps → written checklist via `note.set`. Tick as complete.
5. **Never destroy without looking.** Before deleting/overwriting, look at what's there.
6. **Failed-edit recovery.** Re-read the region, adjust, retry once. Then widen. Full rewrite is last resort — say you fell back and why.

## Step 5 — Verify by observation

Two halves:
- **(a)** The Step 1 criterion passes, **observed** (ran it, saw the output), not inferred from reading code. Use `code.run`, `test.run`, or `ops.run` — not just `fs.read`.
- **(b)** Surrounding system still works: existing tests, build, lint for the touched area.

On failure: mechanical mistake → back to Step 4. Surprise → back to Step 2. **Hard bound: 3 failed fix-verify cycles on the same issue → stop.** Report what was tried, actual output, current hypothesis, hand back to user.

If something cannot be verified (no runtime, needs credentials), say exactly that. Never pass an unverified claim as verified.

## Step 6 — Report outcome-first

- First sentence answers "what happened" or "what did you find". Detail comes after.
- No step numbers or method scaffolding in the report. The only method artifact: the INTENT line when behavior changed.
- Include caveats: what was skipped, what's still weak, what couldn't be verified.
- Clean up scratch files and test artifacts. Note the cleanup.
- Follow-ups only if they emerged from this task. None emerged → end without them.
- Before sending: reread as a hostile reviewer. Any claim not verified → verify now or relabel as caveat.

## Modes

**plan** — Steps 0–3 only. Deliver: classification, definition of done, evidence with citations, one recommended approach. Touch nothing.

**audit** — Grade the most recent completed work against the loop. For each step: followed / skipped / faked. For every skip or fake, name the concrete risk. Deliver a short table + the single highest-value fix.

**report** — Apply Step 6 checklist to the answer you were about to send. Rewrite it, don't send the original.
