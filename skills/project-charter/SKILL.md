---
name: project-charter
description: >
  Charter interview for a new project: ask one question at a time (with your
  recommended answer) and compile the answers into the project's LLM wiki as
  its first charter pages (overview, goals, constraints, glossary, decisions).
  Load when the user invokes /charter or wants to charter a new project.
---

# Project charter — interview a new project into its wiki

A new project starts empty. Your job: interview the user about what this
project IS, and compile the answers into the project wiki as its charter —
the set of pages every later run can read to know what it's working on.

The run that loaded this skill tells you the wiki's absolute path. All paths
below are relative to it. Read/write with your normal `fs.*` tools. The wiki
skill's conventions apply (plain markdown, `# Title` first line, kebab-case
filenames, `index.md` catalog, `log.md` entries as `## [YYYY-MM-DD] <op> | <page>`);
load it via `skill.load name="wiki"` if you need the full layout.

## The interview (grilling rules)

Load `skill.load name="grilling"` and follow its loop — it is the doctrine
this interview runs on. In short:

- Ask ONE question at a time via `ask.user`; wait for each answer.
- With every question, give YOUR recommended answer — the user confirms or
  corrects instead of composing from scratch.
- Walk branch by branch: scope before stack, goals before constraints.
- Never ask about facts you can find yourself (existing files, repo state) —
  look them up.

Keep it short: a good charter takes 5–8 questions, not 30. When the user
says "that's enough" or answers get thin, move to compiling.

## The questions (walk this tree)

1. **Purpose** — what is this project, in one sentence? (→ `overview.md`)
2. **Done looks like** — how do we know it worked? What is explicitly a
   non-goal? (→ `goals.md`)
3. **Constraints** — stack, hardware, budget, hard no's. (→ `constraints.md`)
4. **Terms** — project-specific words a stranger (or fresh run) would
   misread. (→ `glossary.md`; skip if none)
5. **Decisions already made** — anything settled before this interview, with
   the why. (→ `decisions.md`; skip if none)

## Compile

After the last answer, write the pages the answers call for (skip any the
user had nothing for), then:

- `index.md` — catalog every page with a one-line summary.
- `log.md` — one entry per page: `## [YYYY-MM-DD] create | <page>`.
- Cross-link pages with relative links (`[goals](goals.md)`).

Charter tone: factual, present tense, no marketing. Each page stands alone —
a run that reads only `constraints.md` gets the full constraint picture.

## Finish

Summarize the charter back in 3–5 lines, list the pages you wrote, and note
that the wiki now seeds the project graph (`graph.build` maps wiki pages, so
later runs can find the charter by asking the graph). Then stop interviewing —
the project is chartered; normal work can start.
