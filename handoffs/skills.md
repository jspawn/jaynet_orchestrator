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

### Optional frontmatter: procedures

Two extra fields turn a skill into a **procedure** the loop enforces, not
just suggests:

```markdown
shape: implement-from-spec        # task-shape tag; a confident keyword match
                                  # auto-loads the body at run start (brain only)
checkpoints:                      # short checkable statements (max 8, ~160 chars)
  - Spec's own check command ran and passed
  - Every deliverable file exists (fs.list)
```

With `shape:`, a request matching the shape's keywords
(`agent.procedure_selector.shapes` in `config/runtime.yaml`) gets the body
injected at run start — small models rarely `skill.load` on their own. With
`checkpoints:`, the loop nudges against that checklist: appended to
stall-ladder rungs and checked once (`procedure_check` event) before a final
answer is accepted. See `skills/implement-from-spec/SKILL.md` for the pattern.
Keep keywords narrow — a false positive injects the whole body for the run.

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
