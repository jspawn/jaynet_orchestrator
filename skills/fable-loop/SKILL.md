---
name: fable-loop
description: Orchestrated execution for complex tasks — parallel evidence subagents, a committed plan, surgical main-thread execution, adversarial verifiers, and an audited report. Load for tasks that fan out or run unattended; load fable-method first for the core rules.
---
# The Fable Loop

Orchestrated execution for complex tasks: parallel evidence subagents → committed plan (stops for approval when irreversible) → surgical main-thread execution → adversarial verifier agents → audited report.

**When to use:** tasks that fan out (multiple files, multiple sources, subagents), run unattended, or where false completion is a real risk. Load `fable-method` first for the core rules — this skill adds the orchestration layer.

**Tools you'll need:** `agent.spawn` (evidence + verifier subagents), `code.delegate` (execution), `verify.score` (quality gate), `note.set` (plan + checklist), `deliver.files` (handoff).

---

## Phase 1 — Parallel evidence gathering

Spawn independent evidence subagents via `agent.spawn`. Each subagent gets:
- A specific, bounded question ("What does the API return for X?", "Read the spec for Y")
- A budget carve-out (keep it small — 3–5 iterations each)
- Instructions to return findings, not to make changes

**Rules:**
- Subagents that don't depend on each other run in parallel (one `agent.spawn` batch)
- Each subagent reports: what it found, what it couldn't find, and any surprises
- You synthesize their findings — don't just concatenate

**When to skip:** task is contained in one file or one data source. Just read it yourself.

## Phase 2 — Plan and commit

Apply fable-method Steps 0–3 on the combined evidence:
1. Classify the ask
2. Define done with named verification
3. Synthesize evidence into ONE plan

Write the plan to `note.set` as a numbered checklist. Each item has:
- What to do
- How to verify it worked
- Whether it's reversible

**Stop gate:** if any step is irreversible or outward-facing (push, deploy, send, delete shared data), present the plan and STOP for user approval. Local-only work proceeds without asking.

## Phase 3 — Surgical execution

Execute the plan item by item. For each:
1. Apply the fable-method intent gate before behavior-changing edits
2. Use `code.delegate` for non-trivial code (the specialist sees only its item, not the whole plan)
3. Verify each item as you go (don't batch all verification to the end)
4. Update the checklist in `note.set` — tick completed items

**If an item fails after 2 attempts:** mark it failed in the checklist, note what happened, continue to the next item unless it's a blocker. Report all failures at the end.

## Phase 4 — Adversarial verification

After execution is complete, spawn a verifier subagent via `agent.spawn`. The verifier:
- Gets the original task + the checklist of what was claimed done
- Does NOT get your reasoning or your assessment of success
- Must independently verify each claim by running/observing, not by reading your report
- Reports: VERIFIED (observed it working) / CAVEAT (partially working or untestable) / REFUTED (claimed done but isn't)

**Verifier prompt template:**
```
The following task was just completed: {original_task}

The executor claims these items are done:
{checklist}

For each item, independently verify by running the relevant test, command, or
observation. Do NOT trust the executor's report — re-run everything yourself.
Report each item as VERIFIED, CAVEAT (with explanation), or REFUTED (with
the actual output you observed vs what was claimed).
```

Additionally, run `verify.score` on the overall deliverable with criteria derived from the Step 1 definition of done.

**Score gate:** if verify.score returns < 0.7 or any checklist item is REFUTED, go back to Phase 3 for those items only. Hard bound: one retry. If still failing after retry, report honestly.

## Phase 5 — Audited report

Apply fable-method Step 6 (outcome-first reporting) with these additions:
- Include the verifier's verdict for each checklist item
- Any CAVEAT items are listed as explicit caveats in the report
- Any REFUTED items that couldn't be fixed are listed as known failures
- Clean up all scratch files, test artifacts, and subagent debris
- Use `deliver.files` for any produced artifacts

**The report is NOT done until the verifier has run.** Never present work as complete before Phase 4.
