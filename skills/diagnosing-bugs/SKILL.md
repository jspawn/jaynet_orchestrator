---
name: diagnosing-bugs
description: >
  Diagnosis loop for hard bugs and performance regressions: build a tight
  pass/fail feedback loop first, then reproduce → minimise → hypothesise →
  instrument → fix with regression test. Load when the user says "diagnose" /
  "debug this", or reports something broken, throwing, failing, or slow.
---

# Diagnosing Bugs

A discipline for hard bugs. Skip phases only when explicitly justified.
Read the project's `AGENTS.md` (if it exists) for a mental model of the modules
involved.

## Phase 1 — Build a feedback loop (THIS is the skill)
If you have a TIGHT pass/fail signal that goes red on *this* bug, you will find
the cause — everything else just consumes it. Without one, no amount of staring
at code saves you. Spend disproportionate effort here.

Ways to construct one, roughly in order:
1. **Failing test** at whatever seam reaches the bug (`code.run` / `test.run`).
2. **curl / HTTP script** against a running service.
3. **CLI invocation** with a fixture input, diffing output against known-good.
4. **Headless browser** (`browser.*`) — drive the UI, assert on DOM/console.
5. **Replay a captured trace** — `trace.query view=events run_id=...` shows what
   a failing run actually did; save real payloads and replay through the code
   path in isolation.
6. **Throwaway harness** — minimal subset of the system exercising the bug path.
7. **Property / fuzz loop** — "sometimes wrong" → 1000 random inputs, look for
   the failure mode.
8. **Bisection harness** — bug between two commits: automate "boot at X, check"
   so `git bisect run` can drive it.
9. **Differential loop** — old vs new version (or two configs), diff outputs.
10. **Human in the loop** — last resort: drive the user via `ask.user` with
    exact steps; their captured output feeds back to you.

Then TIGHTEN the loop: faster (narrow scope), sharper (assert the exact
symptom, not "didn't crash"), more deterministic (pin time, seed RNG, freeze
network). A 2-second deterministic loop is a superpower; a 30-second flaky one
is barely better than none. For non-deterministic bugs the goal is a higher
reproduction RATE — loop the trigger 100×, add stress, narrow timing windows.

If you genuinely cannot build a loop: stop, say so, list what you tried, and
ask the user for environment access, a captured artifact (logs, HAR, core
dump), or permission to add temporary instrumentation.

**Phase 1 is done when** you can name ONE command you have ALREADY RUN that is
red-capable (asserts the user's exact symptom), deterministic, fast (seconds),
and agent-runnable. If you catch yourself building a theory before this command
exists — stop; that is the exact failure this skill prevents.

## Phase 2 — Reproduce + minimise
Run the loop, watch it go red. Confirm it's the failure mode the USER described
(not a nearby different one), and capture the exact symptom. Then shrink the
repro: cut inputs, callers, config, steps ONE at a time, re-running after each
cut. Done when every remaining element is load-bearing — removing any one turns
the loop green. The minimal repro becomes your regression test in Phase 5.

## Phase 3 — Hypothesise
Generate **3–5 ranked hypotheses** before testing any (single-hypothesis
thinking anchors on the first plausible idea). Each must be falsifiable:
"If X is the cause, then changing Y will make the bug disappear." No prediction
→ it's a vibe; sharpen or discard. Show the ranked list to the user before
testing — domain knowledge re-ranks instantly ("we just deployed a change to
#3"). Don't block on it; proceed with your ranking if the user is away.

## Phase 4 — Instrument
Each probe maps to a specific Phase-3 prediction. **One variable at a time.**
Prefer debugger/REPL inspection, then targeted logs at the boundaries that
distinguish hypotheses — never "log everything and grep". **Tag every debug
log** with a unique prefix, e.g. `[DEBUG-a4f2]` — cleanup is then one grep.
For PERFORMANCE regressions, logs are usually wrong: establish a baseline
measurement (timing harness, profiler, query plan), then bisect. Measure first,
fix second.

## Phase 5 — Fix + regression test
Write the regression test BEFORE the fix — but only at a **correct seam** (one
exercising the real bug pattern as it occurs at the call site; a too-shallow
seam gives false confidence). If no correct seam exists, that itself is a
finding — note it. Otherwise: turn the minimal repro into a failing test, watch
it fail, apply the fix, watch it pass, then re-run the Phase-1 loop against the
original un-minimised scenario.

## Phase 6 — Cleanup + post-mortem
Required before declaring done:
- [ ] Original repro no longer reproduces (re-run the Phase-1 loop)
- [ ] Regression test passes (or missing seam documented)
- [ ] All `[DEBUG-...]` instrumentation removed (grep the prefix)
- [ ] Throwaway harnesses deleted
- [ ] The hypothesis that turned out correct is stated in the commit message —
      so the next debugger learns

Then ask: what would have PREVENTED this bug? If the answer is architectural
(no good test seam, tangled callers, hidden coupling), report it to the user
with specifics — after the fix, not before; you know more now.
