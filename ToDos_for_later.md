# To-dos for later

Parked ideas from the Matt Pocock skills evaluation (2026-07). Done and live:
grilling, tdd, diagnosing-bugs, writing-great-skills + /wgs, diff-review.

## Shared-language convention (CONTEXT.md + ADRs)

Port `codebase-design` + `domain-modeling` (+ optionally `grill-with-docs`):
a root `CONTEXT.md` glossary of project domain terms (brain, coder, preset,
boot posture, confinement, work_root, …) and `docs/adr/NNNN-*.md` decision
records, maintained by the agent as it works.

- Why parked: injecting a glossary into every run costs context proportional
  to its size; it only earns that back once it's large and kept current.
- Cheap 80% variant when picked up: write `CONTEXT.md` for orch-dev once,
  have the agent read it *on demand* (not injected), skip the two skills.
  The skills add the maintenance discipline — adopt only if the glossary
  actually drifts.
- Source: /srv/tmp/skills/skills/engineering/{codebase-design,domain-modeling,
  grill-with-docs} (adapt: CONTEXT.md reading → on-demand; ADR offers stay).
