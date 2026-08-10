# Handoff: create a new chain

**Goal:** a fixed, named multi-step workflow — e.g. research → distill,
fetch → summarize → file.

## What a chain is

A YAML file. Built-ins live in `chains/` in the repo; admin-made ones in the
custom layer (`$JAYNET_DATA/custom/chains/`, via Admin → Studio → Chains),
which wins on a name clash. Read `chains/research-brief.yaml` first — it's
the canonical example:

```yaml
description: research a topic on the web and distill a sourced brief
steps:
  - id: research
    agent: |
      Research "{{input}}" on the web. Report the key facts in plain text,
      each with its source URL. Be thorough but concise (max ~400 words).
    tools: [web.search, web.fetch]
  - id: brief
    prompt: |
      Distill the research below into a brief: 5 bullet points, each one
      sentence, keeping the source URLs. End with a one-line bottom line.

      {{steps.research.output}}
    model: local-orchestrator
```

## The rules that matter

- **Two step kinds.** `agent:` spawns a bounded sub-agent (full tool power,
  own budget carve-out, same gating as `agent.spawn` — `tools:` can narrow
  the toolset). `prompt:` is one stateless LLM call for transforming a prior
  step's output.
- **`prompt` steps are local-only by design.** A cloud call inside a chain
  would bypass the privacy/confirmation gate, so cloud aliases are refused
  with a pointer to `llm.call`. Don't try to route around this.
- **Templates are strict.** Only `{{input}}` (the caller's input) and
  `{{steps.<id>.output}}` (a previous step's result) interpolate — anything
  else is a load-time error, not silently passed through.
- **`model:` is optional** on both kinds; the default is the orchestrator
  brain.
- Engine reference: the module docstring at the top of
  `tools/chain/engine.py` documents the exact semantics — read it before
  edge-case designs.

## Create it

- **Studio (recommended):** Admin → Studio → Chains → *+ new chain* — *Draft
  with AI*, *Validate* (checks YAML structure + placeholder correctness),
  *Save*. Live without restart; export/import as `.jaypack`.
- **In the repo:** add `chains/<name>.yaml` when the chain should ship with
  JayNet.

## Verify

Run it in chat: `chain.run(name="<name>", input="<something>")` and watch
the steps in the run view. `chain.list` confirms registration. For repo
chains, the suite covers the engine
(`python -m pytest tests/ -q -k chain`).
