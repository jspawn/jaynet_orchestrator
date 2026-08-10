# Handoff: create a new skill

**Goal:** teach the agent a reusable method, style, or domain playbook.

## What a skill is

A directory with one Markdown file:

```
skills/<name>/SKILL.md
```

```markdown
---
name: my-skill
description: >
  One or two sentences the brain reads to decide WHEN to load this skill.
  Write it as a trigger: what task, what signals. This is the only part
  visible before loading.
---
# Title — the instructions themselves, loaded on demand via skill.load
```

The model sees only the **description** in its tool list; the body enters
context only when the model calls `skill.load("<name>")`. So: description =
when to load, body = what to do. Keep the body tight — it costs context
every time it's loaded.

## Two ways to create one — pick deliberately

1. **Studio (no repo, no restart):** Admin → Studio → Skills → *+ new skill*.
   *Draft with AI* drafts it with the local model, *Validate* checks
   frontmatter/body, *Save* lands it in the custom layer
   (`$JAYNET_DATA/custom/skills/`) — live on the next `skill.load`, survives
   `git pull` deploys, and **overrides a built-in of the same name** (delete
   the custom row to restore the shipped one). Export/import as `.jaypack`.
2. **In the repo (`skills/<name>/SKILL.md`):** for skills that ship with
   JayNet itself. This is the path for a PR.

## Before you write: read the style guide

`skills/writing-great-skills/SKILL.md` is the shipped guide for exactly this
(also triggerable in chat as `/wgs`). Then read `skills/coding/SKILL.md` —
it's the house style in practice:

- imperative, second person, short sections with numbered phases
- reference tools by their exact names (`fs.read`, `code.symbols`, …)
- teach *choreography* (orient → change → verify), not prose about the domain
- context discipline: act on `path:line` handles, never tell the model to
  paste whole files into context

## Verify

- In chat: `skill.list` shows it, `skill.load("<name>")` returns the body,
  and a task matching the description should trigger a load.
- Studio's *Validate* catches structural problems; the suite pins the
  registry mechanics (`python -m pytest tests/ -q -k skill`).
