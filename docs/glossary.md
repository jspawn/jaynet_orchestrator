# Glossary

Agent tooling has no settled vocabulary — every project names the same things
differently. This file is JayNet's canonical naming. One line per term, with
a pointer to where it lives.

## The big picture

- **Harness** — everything around the model that makes it an agent: the loop,
  tools, prompts, memory, budgets, compaction, gates. Models are swappable;
  the harness is what stays. JayNet *is* a harness (plus model ops and a web
  console).
- **Orchestrator** — JayNet's own name for itself; survives in the
  `local-orchestrator` alias and the `scripts/orch` CLI.
- **Brain** — the model driving the reasoning loop (default alias
  `local-orchestrator`). Swap it via presets or `/imp`.
- **Specialist** — a second model the brain loads for a task class (coding,
  research, security) and hands back from. Up to three boot slots.
- **Alias** — the LiteLLM model name everything references
  (`local-orchestrator`, `local-specialist`, `glm-5.2`, …), never a file path.

## Models & serving

- **Preset** — the admin-managed description of one model: alias, port, GPU,
  and either a `.conf` (GGUF path + llama-server flags, for models JayNet
  launches) or a remote endpoint (adopted servers ship no `.conf`).
  Managed in Admin → Presets ([models.md](models.md)).
- **Boot slot** — which preset runs permanently: brain, specialist1–3, embed,
  rerank. All but brain may be empty ([model-placement.md](model-placement.md)).
- **Remote preset** — an already-running OpenAI-compatible server (llama.cpp,
  vLLM, Ollama) on another LAN box, treated like a local preset but never
  launched/stopped by JayNet — probe only.
- **llama-server** — the inference binary (llama.cpp) serving one GGUF per
  process; JayNet launches and swaps these.
- **LiteLLM proxy** — the OpenAI-compatible front door (default `:4000`)
  unifying local and cloud models behind aliases; enforces the master key.
- **served_id** — the name a running llama-server reports (its `--alias`);
  matched against what the preset claims to serve.
- **Quant** — the GGUF compression level (`Q4_K_M`, `IQ4_XS`, …): smaller =
  faster + dumber.
- **Cloud model** — an external provider alias; approval-gated and blocked by
  taint (below).

## Agent runtime

- **Run** — one message in → agent loop → answer out. Replayable step by step
  in Admin → Status.
- **Iteration** — one model turn inside a run (model call + its tool calls).
- **Tool** — a namespaced action the model may call (`fs.read`, `web.search`,
  …). ~110, plugin-discovered from `tools/` ([catalog.md](catalog.md)).
- **Slash command** — `/<something>` typed by the user. Most bypass the model
  entirely (`/help`, `/goal`, `/imp`, `/compact`, `/<tool>` runs one tool
  directly).
- **Fast-path** — canned replies with zero model use: greetings, and the
  smoke test.
- **Smoke test** — a bare `test` as the very first message: answered by a
  model-endpoint liveness probe, not an agent run.
- **`/imp` (impersonate)** — user-bound temporary brain override (any running
  local or configured cloud model); `/impstop` switches back.
- **Goal (`/goal`)** — a user-bound objective pursued one turn per run until
  its done-criterion holds.
- **Todos** — the visible plan a multi-step run works through, shown in the
  chat's side tab.
- **Compaction** — summarizing a conversation that outgrows the context
  window; salience-weighted, older turns collapse first.
- **Sub-agent** — a child run via `agent.spawn`, own budget, reports back.
- **Council** — `council.debate`: several models argue, one synthesizes.
- **Budget** — per-run caps: iterations, wall clock, tokens, cost. Admin sets
  the house default; users can narrow it.
- **Watchdog / coroner report** — postmortem written when a run gets stuck or
  fails; surfaces in the admin Flags tab.
- **Trace (`trace.db`)** — every run's steps, logged; the source for Status
  replay and `trace.mine` pattern mining.
- **Trajectory** — the compact tool-call sequence of a run
  (`web.search → web.fetch → …`).

## Extending JayNet

- **Skill** — a markdown playbook the agent loads on demand (`skill.load`).
  Naming one in your message ("use the X skill") pins the load mechanically:
  the web layer injects a force-load directive into the run, so the brain
  can't skip it.
- **j-space** — the shipped deliberate-workspace skill: classifies a task
  (fast / full / loop), routes to one focused module, and keeps a ledger of
  what is settled/open/next for long work. A prompt-doctrine suite (adapted
  from the Apache-2.0 J-Space Cognition Suite), NOT the interpretability
  "J-lens" research it borrows its vocabulary from — nothing here reads
  model internals. Its active pass shows as a run badge in chat.
- **Run badge (`run.badge`)** — a short live label on a run's footer and in
  the debug view (Ctrl+D), set by skills to show which mode is active.
- **Subcall (`llm_query`)** — a mediated sub-LLM completion from inside a
  `code.execute` snippet (helpers `llm_query` / `llm_query_batched`, injected
  when the run grants them): billed to the run's budget, taint-gated to local
  models, capped per execution, traced as `subcall` events. The RLM primitive.
- **RLM (Recursive Language Models)** — the "context-as-variable" pattern:
  keep the long document in files, slice it programmatically, map subcalls
  over the slices, reduce yourself — instead of hauling bulk into the context
  window or compacting it away. Native in JayNet (no plugin): workspace files
  + `code.execute` + `context.stage` + subcalls; the `long-document` skill
  teaches the doctrine ([arxiv.org/abs/2512.24601](https://arxiv.org/abs/2512.24601)).
- **Context staging (`context.stage`)** — moving oversized text out of the
  conversation into a workspace file (content-hashed, idempotent), leaving a
  path to address programmatically.
- **Chain** — a small YAML pipeline of steps/tool calls.
- **Connector** — a declarative YAML API integration (no Python).
- **MCP bridge** — `mcp.list` / `mcp.call` to Model Context Protocol servers.
- **Custom layer** — user-built skills/chains/tools/connectors/evals under
  `<data>/custom/`; survives git pulls, wins name clashes with built-ins.
- **Studio** — the admin UI for building custom-layer items, with AI
  drafting ([studio.md](studio.md)).
- **.jaypack** — the zip export/import format for sharing custom-layer items.
- **Plugin** — an optional, toggleable capability bundle (tools + skills +
  hooks + routes in one package). Ships disabled by default; enabled in
  Admin → Plugins. Disabled or broken plugins are never imported, so they
  can't take JayNet down ([plugins.md](plugins.md)).
- **Project graph (`graph.*`, graphify plugin)** — an auto-built map of one
  project's code and docs as traversable nodes/edges; the agent queries it
  (`graph.query`/`graph.explain`/`graph.path`) instead of reading whole
  files. Lives at `<project>/graphify-out/`, deleted with the project. Not
  the kg knowledge graph below — this one is derived, not curated.
- **Wiki (`/llmwiki`)** — curated memory pages the agent maintains; global or
  per-project (deleted with the project).
- **Charter (`/charter`)** — a project wiki's first pages (overview, goals,
  constraints, glossary, decisions), compiled from a short interview when a
  project is created; offered by the new-project dialog.

## Data & memory

- **Memory (`memory.*`)** — persistent notes across chats.
- **Knowledge graph (`kg.*`)** — the curated store of facts and relations
  you or the agent teach it, across chats. Not the graphify project graph
  above — this one is curated, not derived.
- **RAG** — retrieval over your own documents: embed (`:8095`) + optional
  rerank (`:8096`) models, both CPU.
- **Collection** — one RAG document set.
- **Project** — a workspace with its own files, chats and wiki; deleted as a
  unit.
- **Chat scratch** — per-chat temporary file area, auto-swept.
- **Outputs / deliverables** — files a run produces; they expire unless you
  save them.
- **Saved chat** — chats are unsaved by default (per-browser); the save
  button or the user-menu toggle keeps them.

## Privacy & safety

- **Taint** — output of a private tool marks the conversation; while tainted,
  nothing leaves for the cloud unless you explicitly share it.
- **Cloud gate** — cloud calls need approval up front; local models are the
  default for everything.
- **Confirmation** — destructive tools ask before acting.
- **Sandbox** — `code.run` / `test.run` execute under firejail (Linux), or in
  a per-run podman **devbox** container when `tools.code.devbox` is enabled —
  a toolchain image (Rust, Go, Node, C/C++, Java, .NET) so builds don't
  depend on the host.

## Evals & quality

- **Eval case** — a YAML test for the harness: prompt + expectations, stored
  like chains (`evals/` built-in, custom layer for your own). Beyond the
  judge rubric, a case can carry deterministic graders (`answer_exact_any`
  exact match, a `checker` script) and an optional podman `container` to run
  inside.
- **Eval run / suite** — cases executed through the real agent loop, scored
  per case.
- **Judge** — the model that scores eval answers (config:
  `eval.judge_model`).
- **Driver** — the model that turns failures into concrete fix proposals.
- **Proposal** — a suggested fix (prompt, skill, tool description, config)
  applied to the custom layer with one admin click; the next suite measures
  the effect.
- **Benchmark** — fixed-seed comparison runs across models (same harness, no
  proposals) — answers "which brain is better *here*?".
- **benchlab** — the opt-in plugin that imports *public agent benchmarks*
  (Terminal-Bench, GAIA) as eval cases. Distinct from the Benchmark sub-tab
  above: benchlab supplies standardized cases; the Benchmark sub-tab compares
  brains over any suite.
- **Flag** — a user marks a bad session (optionally including private
  context); lands in the admin Flags tab and can become a new eval case.
