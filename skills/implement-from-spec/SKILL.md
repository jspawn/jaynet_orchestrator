---
name: implement-from-spec
description: >
  Implement an algorithm, conversion, or attack from a given spec, paper, or
  task description where success is judged by concrete checks on delivered
  files (typical benchmark shape: "write /app/x that passes the tests").
  Load when the task names exact output files, test criteria, or a reference
  document to implement. The procedure frontier models run implicitly:
  restate → smallest end-to-end version → run → iterate on real errors →
  verify against the spec's OWN checks.
---
# Implement from spec — the procedure

Small models lose these tasks by skipping steps, not by lacking knowledge.
Run the steps in order. Do not skip verification — "looks right" fails.

## 1. Extract the contract (one pass, write it down)
Before any implementation, `note.set` with:
- **Deliverables**: every file/path the task names, verbatim.
- **Checks**: how success is measured — the task's own test commands, expected
  values, formats, tolerances. If the task ships tests, READ them first:
  they are the spec made executable (expected CLI args, output format,
  edge cases). Implementing against the prose while the tests expect
  something else is the #1 way these runs fail.
- **Unknowns**: what the spec leaves open. Pick the simplest interpretation;
  write your assumption next to it.

## 2. Smallest end-to-end version (within the first few turns)
Write the dumbest complete version that produces the deliverable in the right
shape — hardcode intermediate steps if needed. Then RUN it immediately.
A concrete error from a real run is worth more than ten minutes of planning.
Heavy math/algorithm core? This is the moment for `code.delegate` — hand the
specialist the contract from step 1, not your half-formed plan.

## 3. Iterate on real errors only
- One change per run. Reproduce → change → re-run → read the ACTUAL error.
- Same failure twice → different strategy (simplify, other library/algorithm,
  tiny input first). Never re-issue with tweaked args.
- Keep a running `note.set`: what works, what's still red, current hypothesis.

## 4. Verify against the spec's OWN checks
- Run the task's test command if one exists — your own checks are a
  supplement, never a replacement.
- Deliverable format: diff your output against the expected schema/example
  field by field (right values in the wrong format is a fail).
- Confirm every file from step 1 exists with `fs.list` before finishing.

## 5. Escalation rungs (when stuck, in order)
1. Re-read the spec/tests — the answer you missed is usually there.
2. `web.search` the algorithm/paper name for a reference implementation to
   check your understanding against (not to copy blindly).
3. `code.delegate` the stuck unit with your notes from step 3.
4. Still blocked → tell the user exactly what blocks you and ask.

## Anti-patterns (each seen failing real runs)
- Reading the input files, then stopping without writing anything.
- Declaring done because the code runs — without running the spec's checks.
- Pasting the solution into chat instead of writing the named file.
- Spending the whole budget perfecting step 2's analysis; start ugly instead.
