# Documentation index

*Start here — the docs routed by reader. Each destination doc carries its
own one-line audience header under the title.*

## I'm using the chat

- [playbook.md](playbook.md) — the honest tour: every tool family, what
  it's good at, where the pieces harmonize or compete.
- [catalog.md](catalog.md) — generated list of all tools, skills, chains
  and slash commands.
- [studio.md](studio.md) — extending JayNet from the browser (custom
  tools, skills, chains) without touching the repo.
- [glossary.md](glossary.md) — the house terms (brain, specialist,
  dispatch, j-space, …) in one place.

## I operate an install

- [setup_installation.md](setup_installation.md) — the guided installer
  path; [manual_installation.md](manual_installation.md) — everything by
  hand (advanced); [upgrading.md](upgrading.md) — moving versions.
- [operations.md](operations.md) — day to day: logs, traces, spend,
  backups, troubleshooting.
- [models.md](models.md) — recommended license-clean models per role;
  [model-placement.md](model-placement.md) — GPU/CPU topology, swaps and
  co-tenancy; [llama-ops.md](llama-ops.md) — llama.cpp server operations
  incl. measured scheduling.
- [configuration.md](configuration.md) — orientation map of runtime.yaml;
  [admin.md](admin.md) — the admin console tab by tab.
- [security.md](security.md) — the posture and the accepted risks, stated
  plainly; [plugins.md](plugins.md) — the bundled plugins.
- [brain-bakeoff.md](brain-bakeoff.md) / [clm-bakeoff.md](clm-bakeoff.md) —
  the measured model comparisons, for tuning decisions.
- [my-setup.md](my-setup.md) — the author's own box, clearly marked
  example-only.

## I contribute code

- [architecture.md](architecture.md) — how the pieces fit;
  [code-map.md](code-map.md) — where each piece of logic lives, file by
  file.
- [turn.md](turn.md) — the exact execution order of one loop iteration;
  [rails.md](rails.md) — the generated registry of every guard/gate.
- [development.md](development.md) — running the suite, CI gates,
  conventions; [testing.md](testing.md) — what every test file covers;
  [testing-harness.md](testing-harness.md) — the `test.run` harness.
- [api.md](api.md) — the stable HTTP contract (also for integrators);
  [plugins.md](plugins.md) — the plugin-authoring half.

## I'm an AI session

- [CONTEXT.md](../CONTEXT.md) — the root glossary and project shape, made
  for you.
- [code-map.md](code-map.md) + [turn.md](turn.md) + [rails.md](rails.md) —
  orientation before touching behavior.
- [development.md](development.md) — the gates your change must pass;
  [LEARNING_GUIDE.md](../LEARNING_GUIDE.md) — the design rationale (Part 2
  is history; Part 1 is current design).
- [handoffs/](../handoffs/) — task-type briefings (web UI, skills, chains,
  tools) with the key paths and verification steps.
