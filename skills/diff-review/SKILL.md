---
name: diff-review
description: >
  Review the changes since a fixed point (commit, branch, tag, or merge-base)
  along two axes — Standards (repo conventions + a Fowler smell baseline) and
  Spec (does the diff match what was actually asked for) — as two parallel
  agent.spawn sub-agents, reported side by side. Load when the user wants a
  branch, PR, or work-in-progress reviewed, or says "review since X". For
  auditing an unfamiliar repo as a whole, use codebase-review instead.
---

# Diff review — two axes, kept separate

Review `git diff <fixed-point>...HEAD` on two independent axes:

- **Standards** — does the code follow this repo's documented conventions,
  plus a fixed smell baseline?
- **Spec** — does the diff faithfully implement what was asked for — no more,
  no less?

A change can pass one axis and fail the other (clean code, wrong feature; or
right feature, convention-breaking). Report them separately so neither masks
the other.

## 1. Pin the fixed point

Whatever the user said — a SHA, branch, tag, `HEAD~5`. If they didn't specify,
propose a candidate from `git.log` and confirm via `ask.user`.

Validate BEFORE spawning anything: `git.diff(ref="<point>...HEAD", max_lines=10)`
must succeed and be non-empty (three-dot = against the merge-base). A bad ref
or empty diff fails here, not inside two sub-agents. Note the commit list via
`git.log`.

## 2. Identify the spec

The spec is the originating request, in this order:

1. The current conversation — distill what the user actually asked for into a
   few bullet requirements; that distillation IS the spec.
2. A path the user passed, or a spec/PRD file under `docs/` matching the work.
3. Nothing found → ask the user. If there is no spec, the Spec child is
   skipped and reports "no spec available".

## 3. Identify the standards sources

Read what the repo documents about how code should be written: `AGENTS.md`
files (root + subdirectories the diff touches), `CONTRIBUTING.md`, any
`CODING_STANDARDS.md`. Pass the list to the Standards child.

On top of that, the Standards axis ALWAYS carries the smell baseline below
(Fowler, _Refactoring_ ch.3). Paste it in full into the child's task — the
child cannot see this skill. Two rules bind it:

- **The repo overrides.** A documented repo standard beats the baseline; where
  it endorses something the baseline would flag, suppress the smell.
- **Always a judgement call.** Each smell is a labelled heuristic ("possible
  Feature Envy"), never a hard violation. Skip anything tooling enforces.

Smell baseline — *what it is* → *how to fix*:

- **Mysterious Name** — a function, variable, or type whose name doesn't reveal what it does or holds. → rename it; if no honest name comes, the design's murky.
- **Duplicated Code** — the same logic shape appears in more than one hunk or file in the change. → extract the shared shape, call it from both.
- **Feature Envy** — a method that reaches into another object's data more than its own. → move the method onto the data it envies.
- **Data Clumps** — the same few fields or params keep travelling together (a type wanting to be born). → bundle them into one type, pass that.
- **Primitive Obsession** — a primitive or string standing in for a domain concept that deserves its own type. → give the concept its own small type.
- **Repeated Switches** — the same `switch`/`if`-cascade on the same type recurs across the change. → replace with polymorphism, or one map both sites share.
- **Shotgun Surgery** — one logical change forces scattered edits across many files in the diff. → gather what changes together into one module.
- **Divergent Change** — one file or module is edited for several unrelated reasons. → split so each module changes for one reason.
- **Speculative Generality** — abstraction, parameters, or hooks added for needs the spec doesn't have. → delete it; inline back until a real need shows.
- **Message Chains** — long `a.b().c().d()` navigation the caller shouldn't depend on. → hide the walk behind one method on the first object.
- **Middle Man** — a class or function that mostly just delegates onward. → cut it, call the real target direct.
- **Refused Bequest** — a subclass or implementer that ignores or overrides most of what it inherits. → drop the inheritance, use composition.

## 4. Spawn both reviewers

Send BOTH `agent.spawn` calls in one message. Children see none of this
conversation — each task must be standalone: repo path, the exact ref range
(`<point>...HEAD`), the commit list, and what to return. Restrict each child
to read-only tools: `tools=["git.diff","git.log","fs.read","fs.grep"]`.

Two children on the default local brain share one GPU and run sequentially —
still correct, just slower. Pass a cloud `model` (e.g. `glm`) for real
parallelism when the run allows it.

**Standards child task** — include the standards-source file list and the full
smell baseline, then the brief: "Report — per file/hunk where relevant — (a)
every place the diff violates a documented standard: cite the standard (file +
the rule); and (b) any baseline smell you spot: name it and quote the hunk.
Distinguish hard violations from judgement calls — documented-standard
breaches can be hard, but baseline smells are always judgement calls, and a
documented repo standard overrides the baseline. Skip anything tooling
enforces. Under 400 words."

**Spec child task** — include the distilled spec (or spec file contents), then
the brief: "Report: (a) requirements the spec asked for that are missing or
partial; (b) behaviour in the diff that wasn't asked for (scope creep); (c)
requirements that look implemented but where the implementation looks wrong.
Quote the spec line for each finding. Under 400 words."

If there is no spec, skip the Spec child and say so in the final report.

## 5. Aggregate

Present the two reports under `## Standards` and `## Spec` headings, verbatim
or lightly cleaned. Do NOT merge or rerank findings across axes. End with a
one-line summary: total findings per axis, and the worst issue within each —
never a single overall winner.
