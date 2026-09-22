---
name: coupled-systems
shape: coupled-systems
checkpoints:
  - Hard constraints and invariants listed, separated from preferences (note.set)
  - Dependency map written: which parts read/change which shared state
  - Every change planned as propagation, not isolated edits
  - Relationships re-verified after each change, not just the changed part
  - Global feasibility stated: all hard constraints hold simultaneously
description: >
  Coordinate a task whose parts depend on each other — a migration, a
  cross-cutting refactor, a plan or schedule with shared resources, a
  multi-component design. Load when a locally correct change can silently
  break a distant part, when several modules must agree on the same facts,
  or when the task is one coupled whole rather than independent subtasks.
  The procedure frontier models run implicitly: constraints first →
  dependency map → change-by-propagation → relationship-level verification
  → whole-system feasibility.
---
# Coupled systems — the procedure

Small models lose these tasks by solving each part correctly in isolation
while the WHOLE quietly becomes impossible: a fix that doesn't propagate, two
modules holding different versions of the same fact, a hard constraint
forgotten after step three. A locally valid answer is not a globally feasible
system. Run the steps in order.

## 1. Constraints before solutions
- List the HARD constraints and invariants (what must hold at every step —
  budgets, interfaces, deadlines, data shapes) separately from preferences
  (what is merely nice). `note.set` the list; it is the acceptance bar.
- If two constraints contradict: STOP, name the contradiction, ask which is
  authoritative. Never silently relax one side.

## 2. Map the relations
- Write the dependency map: which parts read or change which shared state,
  who calls whom, what consumes this output. One line per edge is enough —
  keep it in the note from step 1.
- Mark what must stay UNRELATED. Forcing independent parts into one
  structure (over-coordination) is a failure mode too: couple only what the
  constraints actually join.

## 3. Change by propagation, never in isolation
- Every change is: local edit → which dependents does this reach? →
  re-evaluate each against the constraint list → updated coherent whole.
- A change that breaks a constraint is not "mostly done" — propagate until
  every dependent holds, or revert and pick a different change.
- Multi-file code changes → delegate the coupled unit to
  `specialist.delegate` with the constraint list and the dependency map,
  not just the file path.

## 4. Verify relationships, not just parts
- After each change, check the EDGES, not only the node: do both sides still
  refer to the same underlying state? Did the upstream change reach every
  downstream dependent? Do the hard constraints from step 1 still hold?
- Verify by execution where possible (`code.run` / `code.check`), not by
  re-reading your own edit.

## 5. Close with global feasibility
- Before the final answer, state in one breath: every hard constraint holds
  SIMULTANEOUSLY, and name the evidence (checks run, edges verified). If you
  cannot, the task is not done — return to step 3.
- Coordination organizes reasoning; it does not replace evidence. A
  consistent system built on a wrong assumption is consistently wrong —
  falsify the load-bearing assumption before you trust the structure.

## Anti-patterns (each seen failing real runs)
- Fixing the visible symptom while the dependent three hops away keeps the
  old value.
- Two parts of the plan silently using different versions of the same fact.
- Quietly swapping a hard constraint for "close enough" halfway through.
- Optimizing one component past the point where the whole stops fitting.
- "Looks consistent" without one executed check over the coupled unit.

---
Distilled from the coordination framework of
[Principia Structurae Realitatis](https://github.com/CosmicUndercurrent/Principia-Structurae-Realitatis)
(goal → conditions → relations → feasible structure → decision → feedback →
re-coordination), recast from a prompt ritual into loop-enforced steps.
