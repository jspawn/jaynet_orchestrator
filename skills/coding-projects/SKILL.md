---
name: coding-projects
description: Build, implement, or refactor a multi-file or multi-step software project without blowing the run budget. Load when asked to "build/implement/create/refactor" something that spans several files or steps, or whenever a coding task is too big to finish in one pass.
---
# Coding projects — plan, then build one unit per run

A single run accumulates context every iteration (the whole transcript — system
prompt, every file you read, every diff, every test log — is re-sent to the model
each turn) and is capped by a cumulative token budget. So trying to build a whole
project in one loop reliably hits the ceiling with half-finished work. The fix is
structural: **decompose, persist to disk, and let context reset between units.**

## 1. Work inside a project

Your fs.* and code.* tools are already rooted in the project's files directory —
write paths relative to it. That directory — not the chat — is the memory that
survives between runs. If you aren't
in a project yet and the task is non-trivial, say so: the user can create one from
this chat ("from chat" button) or by saying "create a project".

## 2. Plan first — don't touch code yet

Before writing any code, write two files into the project:

- **`PLAN.md`** — an ordered list of small, independently-completable **units**.
  Each unit: touches only a few files, has a one-line goal, and a concrete **done
  check** (a command to run, a test that passes, or an observable behaviour). Size
  each unit to finish comfortably inside one run's budget — if a unit looks like it
  needs many file rewrites, split it.
- **`CHECKLIST.md`** — the same units as `- [ ]` checkboxes, so progress is visible
  in the project file tree.

Planning is cheap (almost no context). Stop after planning and tell the user the
plan, unless they've asked you to proceed straight through.

## 3. Build one unit per run

For each unit, ideally as its own run/turn:
1. Read `PLAN.md` + `CHECKLIST.md` and **only the specific files this unit needs**.
2. Make the change. Prefer `fs.edit` (targeted, unique-string patches) over
   rewriting whole files — a full rewrite puts the entire file in context twice.
3. Verify against the unit's done check (`test.run` a focused test, or run the
   relevant command via `job.start`). Don't dump full logs — grep for failures.
4. Tick the box in `CHECKLIST.md` and note anything the next unit needs to know.
5. Stop. Let the next unit start with a fresh context.

## 4. Keep context lean (this multiplies how much you get done per run)

- Locate code with `fs.grep`; read with line ranges, not whole files.
- Never paste large file contents or full test output back into your answer.
- Parse big tool outputs with `code.execute` instead of reading them inline.
- Spawn a sub-agent for heavy exploration ("find everywhere X is used; return a
  short list") so the raw reads stay out of your context — but note a child's
  budget is carved from *your remaining* budget, so it isolates context, it
  doesn't grant more total room.

## 5. If you get a BUDGET NOTICE

You're near a ceiling. Don't start new work. Save what you have, update
`CHECKLIST.md`, write `NEXT_STEPS.md` (what's done, what's left, how to resume),
summarize for the user, and stop. The next run picks up from those files.

## 6. Definition of done

The project is done when every box in `CHECKLIST.md` is ticked and each unit's done
check passes. Hand the result back with `deliver.files` if the user wants the files
out, or just leave them in the project for continued work.
