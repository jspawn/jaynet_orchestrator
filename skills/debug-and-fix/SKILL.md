---
name: debug-and-fix
shape: debug-and-fix
description: >
  Fix a reported bug, failing test, or broken build where success is the
  check going green again. Load when the request centers on something that
  FAILS — a test suite, a script, an error message. The procedure frontier
  models run implicitly: reproduce the real failure first → smallest fix for
  the root cause → re-run the SAME check → check the blast radius.
---
# Debug and fix — the procedure

Small models lose these tasks by fixing against a DESCRIPTION of the bug
instead of the actual failure, and by stopping at "looks right". Run the
steps in order. The failing check is the spec — never weaken it to pass.

## 1. Reproduce first (before changing anything)
Run the failing test / command / script and capture the ACTUAL error output
verbatim. No reproduction, no fix — a fix against a guessed cause is how
runs make things worse. If the failure needs a specific input or state,
build the smallest version of it. `note.set` the error text + your first
hypothesis.

## 2. Locate the root cause, not the symptom
- Read the code at the error site, then follow the chain UP: who calls this
  with what? The symptom's line is rarely the bug's line.
- One hypothesis at a time, cheapest test first (a print, a one-line probe
  via `code.run`).
- Anything beyond a one-line fix → `code.delegate` with the reproduction
  and your notes from step 1, never your half-formed theory.

## 3. Fix ONE thing
Smallest change that addresses the root cause. Re-run after every change —
never stack two fixes untested. Same failure twice after a change → your
hypothesis is wrong; go back to step 2 with the new evidence instead of
tweaking harder.

## 4. Verify — the same check, then the neighborhood
- Re-run the EXACT failing command from step 1. Green is the bar.
- Then run the surrounding suite / the importers of what you touched — a
  fix that breaks a neighbor is not a fix.
- If the check itself looks wrong (spec contradicts test): STOP and name
  the contradiction. Never edit the test to make the code pass unless the
  user explicitly rules the test is wrong.

## 5. Escalation rungs (when stuck, in order)
1. Re-read the full error — the hint you skipped is usually in the middle.
2. `web.search` the exact error message (with the library/version).
3. `code.delegate` the stuck unit with reproduction + notes.
4. Still blocked → report what you ruled out and ask.

## Anti-patterns (each seen failing real runs)
- Editing code before ever running the failing check.
- "Fixed it" without re-running — the run ends, the bug stays.
- Weakening/deleting the failing test so the suite goes green.
- Fixing the line that crashed while the wrong value came from three
  callers up.
- Re-issuing the same fix with tweaked args after it already failed twice.
