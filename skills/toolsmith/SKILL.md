---
name: toolsmith
description: When you catch yourself repeating the SAME mechanical multi-step computation or procedure several times within a run, distil it once into a small, VERIFIED helper script in the workspace and reuse it instead of redoing the steps by hand. Load on recognising in-run repetition of a rote task (parsing, transforming, extracting, checking, generating). NOT for working around a failing tool (diagnose or pivot instead), and NOT for planning a build (coding-projects) or editing a codebase (coding).
---

# Toolsmith: turn a repeated step into a small, verified helper

You already have `fs.write` and `code.run` (bash or `language=python`) — everything needed to
build your own throwaway tools mid-run. When the same mechanical step keeps coming
up, stop hand-repeating it: write it once, prove it works, and call it.

## When this applies — and when it does NOT

Reach for this when, **within a single run**, you catch yourself doing the *same
mechanical operation a third time* — reformatting the same kind of data, pulling
the same fields, applying the same transform, running the same multi-command
check. The tell is repetition of a **procedure**, not a one-off.

Do NOT reach for this when:

- **A tool is failing.** Failure is not a cue to build a workaround — it's a cue to
  diagnose, pivot to a different tool, or ask the user. Manufacturing a helper to
  route around a broken tool hides the real bug and risks faking success. This is
  the important one: build helpers from what *works*, never to paper over what
  doesn't.
- **It's a one-off.** A helper you use once is pure overhead — just do it directly.
- **It's really a project** (use `coding-projects`) or **editing an existing
  codebase** (use `coding`). This skill is for small, self-contained helpers,
  not features.

## The loop

1. **Recognise.** "I've now parsed three of these the same way." That repetition is
   the signal — don't do it a fourth time by hand.
2. **Write the helper** with `fs.write` — a small, single-purpose script in the
   workspace (e.g. `helpers/extract_fields.py`). Keep it tiny, pure, and
   parameterised: read args or stdin, print the result, no hidden state.
3. **Verify it before you trust it.** Run it once with `code.run`
   on an input whose answer you already know, and confirm the output matches. An
   unverified helper is worse than doing the task by hand — it can be confidently
   wrong on every case. If it doesn't pass, fix it or abandon it; never make a
   helper "pass" by weakening the check.
4. **Reuse it.** Call it with `code.run` for each remaining case instead of
   re-reasoning the steps. It runs sandboxed (no network, workspace-confined) — the
   same gating as any code you run.
5. **Keep it only if it earns it.** If the helper is likely useful in *future* runs
   (not just this one), record one line in `memory.*`: what it does and when to use
   it — e.g. "helper extract_fields.py: pulls {issuer, coupon, maturity} from the
   FMT bond PDFs; re-create from this note if gone." A later run can then
   re-materialise it. Do not persist unverified or single-use helpers — that just
   pollutes memory.

## Guardrails

- **Verify before every reuse.** The entire value is that the helper is
  *known-good*. Skip the check and you've built a reliable way to be wrong.
- **Small and single-purpose.** One helper, one job. A sprawling "utils" grab-bag
  becomes its own maintenance problem.
- **Workspace only.** Helpers live in the run's workspace and run in the sandbox.
  Never write to, or run against, config, the orchestrator's own code, or anything
  outside the workspace.
- **Never build one to satisfy a verifier.** If you're writing a helper so a check
  goes green, stop — that's reward-hacking, not tooling.

## Anti-patterns

- A helper for something you'll do exactly once → just do it.
- A helper to dodge a failing tool → diagnose or pivot instead.
- Trusting a helper you never ran on a known input → verify first.
- Dumping every helper into `memory` forever → keep only durably-useful, verified ones.
