---
name: writing-great-skills
description: >
  Reference for writing and editing skills well — the vocabulary and principles
  that make a skill predictable. Load when creating, reviewing, or pruning a
  skill, or when a skill misbehaves: won't fire, fires on the wrong things,
  rushes steps, or has grown bloated.
---

# Writing Great Skills

A skill exists to wrangle determinism out of a stochastic system.
**Predictability** — the agent taking the same _process_ every run, not
producing the same output — is the root virtue; every lever below serves it.

**Bold terms** are defined in `GLOSSARY.md` (bundled with this skill — its
path comes back in `skill.load`'s `files`; read it when you need a definition).

## How skills work here (the mechanics)

- A skill is `skills/<name>/SKILL.md` under the install root (`ORCH_HOME`):
  frontmatter (`name`, `description`) + the instruction body. Sibling files
  ship as bundled resources — `skill.load` returns their paths in `files`.
- The **catalog** — every skill's name + description — is injected into the
  system prompt of every run. The body is NOT: the model pulls it on demand
  via `skill.load("<name>")`. Skills cross-reference each other by name the
  same way.
- The description is mandatory (the catalog test rejects blank ones) and is
  the only trigger the model sees — this system has no user-only skills.
  The equivalents of "user-invoked": a **forced-load pointer** injected by the
  host via a slash command (`/wgs` force-loads this skill).
- To edit skills, the agent needs a workspace containing the `skills/` tree
  (e.g. the orchestrator dev project) — chat-scratch workspaces can't reach it.

## Invocation and the description

Every skill here is model-invoked, so the description is where invocation is
won or lost. It does two jobs — state what the skill is, list the **branches**
that should trigger it — and every word of it sits in every run's context
(**context load**), so prune it even harder than the body:

- Front-load the skill's **leading word** — that's where it does its work.
- One trigger per branch. Synonyms renaming a single branch are **duplication**
  — collapse them.
- Cut identity already stated in the body. Keep triggers, plus any "load when
  another skill needs…" reach clause.

## Information hierarchy

Two content types — **steps** and **reference** — placed on a ladder by how
immediately the agent needs them:

1. **In-skill steps** — ordered actions, the primary tier. Each ends on a
   **completion criterion**: make it checkable (done vs not-done is decidable)
   and, where it matters, exhaustive — a vague criterion invites
   **premature completion**.
2. **In-skill reference** — rules/facts consulted on demand. A legitimately
   flat peer-set is fine, not a smell.
3. **Disclosed reference** — pushed into a sibling file behind a
   **context pointer**, loaded only when the pointer fires (this skill
   discloses its definitions to `GLOSSARY.md`). The pointer's _wording_
   decides how reliably the agent reaches the material — sharpen wording
   before inlining.

**Progressive disclosure** is the move down the ladder. The branch test:
inline what every branch needs, disclose what only some reach. **Co-location**
decides what sits beside what: a concept's definition, rules, and caveats
under one heading.

## When to split

Each cut costs something — split only when it earns it:

- **By invocation** — a new skill needs a distinct leading word that should
  trigger it on its own, or another skill must reach it. The price is another
  always-loaded description in the catalog.
- **By sequence** — split a run of steps when the steps ahead tempt the agent
  to rush the one in front of it.

## Pruning

- **Single source of truth**: each meaning lives in one place.
- Check every line for **relevance**: does it still bear on what the skill does?
- Hunt **no-ops** sentence by sentence: does this change behaviour versus the
  model's default? If not, delete the sentence — don't trim it.

## Leading words

A **leading word** is a compact concept already in the model's pretraining
(_tight_ loop, _red_, _tracer bullet_) that anchors a region of behaviour in
one token. In the body it anchors execution; in the description it anchors
invocation — use the words you actually say when you want the skill. Hunt for
restatements begging to collapse into a single token.

## Failure modes (diagnostics)

- **Premature completion** — a step ends before it's done. Sharpen the
  completion criterion first; split the sequence only if the rush persists.
- **Duplication** — one meaning in two places.
- **Sediment** — stale layers that settle because adding feels safe.
- **Sprawl** — too long even though every line is live; cure is the ladder.
- **No-op** — a line the model already obeys by default.
- **Negation** — steering by prohibition names the elephant. Prompt the
  positive; keep prohibitions only as hard guardrails, paired with what to
  do instead.
