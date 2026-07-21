---
name: tdd
description: >
  Test-driven development — the red → green loop with tests worth keeping:
  behavior through public interfaces, pre-agreed seams, vertical slices.
  Load when building features or fixing bugs test-first, when the user mentions
  TDD or red-green-refactor, or when a change needs durable test coverage.
---

# TDD — red → green, one vertical slice at a time

Read the project's `AGENTS.md` (if it exists) so test names and interface
vocabulary match project conventions.

## What a good test is
Tests verify BEHAVIOR through public interfaces, not implementation details.
Code can change entirely; tests shouldn't. A good test reads like a
specification ("user can checkout with valid cart") and survives refactors
because it doesn't care about internal structure. Expected values come from an
independent source of truth — a known literal, a worked example, the spec.

## Seams — where tests go
A **seam** is the public boundary you test at: where you observe behavior
without reaching inside. Tests live at seams, never against internals.

**Test only at pre-agreed seams.** Before writing any test, write down the
seams under test and confirm them with the user (`ask.user`): "What's the
public interface, and which seams should we test?" Agreeing seams up front is
how testing effort lands on critical paths instead of every edge case.

## Anti-patterns (never write these)
- **Implementation-coupled** — mocks internal collaborators, tests private
  methods, asserts call counts, or verifies through a side channel (querying
  the DB instead of the interface). The tell: the test breaks on refactor
  though behavior didn't change.
- **Tautological** — the assertion recomputes the expected value the way the
  code does, so it passes by construction and can never disagree.
- **Horizontal slicing** — all tests first, then all implementation. Bulk tests
  verify *imagined* behavior and lock in structure before you understand the
  implementation. Work in vertical slices: one test → one implementation →
  repeat, each test a tracer bullet responding to what the last cycle taught you.

## Mocking
Mock at **system boundaries only**: external APIs, time/randomness, sometimes
DB/filesystem (prefer a real test DB/tmp dir). Never mock your own modules or
internal collaborators — that's the implementation-coupled smell. If a boundary
is hard to mock, inject the dependency (pass the client in) rather than
patching internals.

## The loop (per slice)
1. **Red** — write the failing test at the agreed seam; run it and WATCH it
   fail: `code.run ".venv/bin/python -m pytest tests/test_x.py::test_y -q"`.
2. **Green** — write only enough code to pass it. No speculative features, no
   anticipating future tests.
3. **Verify** — `lint.run` the touched files; re-run the test.
4. **Commit** — small, working unit (`git.add` / `git.commit`). Then the next
   slice.

Refactoring is NOT part of the loop — defer it to a final review pass once the
slices are green.
