# JayNet Playbook

A plain-language map of what JayNet actually has, what each part is good at,
how the pieces play together — and where they get in each other's way.
Written against the v1.1.0 code (plugin system + graphify); every claim here
was checked against the implementations, not just the descriptions.

---

## 1. What JayNet is

JayNet is a **local-first LLM orchestrator**: one service on your machine, a
web console in the browser, and an **agent loop** at the center. You talk to a
small, fast "brain" model (a low-active-parameter MoE) that is cheap to run
locally; when a task needs muscle, the brain doesn't try harder — it
**delegates**: to a dense specialist coder on the second GPU, to sub-agents,
or (approval-gated) to a cloud model.

Four ideas run through everything:

- **Local first, cloud by explicit choice.** Every tool that talks to a
  remote LLM is confirmation-gated, and results from "private" tools (files,
  memory, graphs…) taint the run: they never leave the box to a cloud model
  without a human approving that specific call.
- **Context is the scarce resource.** The loop runs budgets, compacts old
  history into briefs, and re-injects anchors (goal, note, todos) so long
  runs don't forget what they're doing. Heavy transcripts are pushed into
  sub-agents so the main thread stays lean.
- **State over cleverness.** Anything that must survive a run — memory,
  knowledge graph, RAG chunks, research claims, traces, eval results,
  schedules — lives in small local SQLite DBs. No vector DB service, no
  external state.
- **Humans stay in the loop.** Writes, git mutations, jobs, cloud calls and
  MCP calls pause for approval. A decline is final.

The building blocks: **tools** (things the agent can *do*), **skills**
(how-to documents the agent *reads* on demand), **chains** (fixed YAML
pipelines), **plugins** (toggleable bundles of tools+skills+hooks+routes —
new in 1.1.0), plus **Studio**: the admin UI for creating custom versions of
all of them outside the git tree.

---

## 2. The pieces

### 2.1 Tools — the agent's hands

113 shipped tools in ~30 namespaces (`docs/catalog.md` has the full list),
plus plugin tools when enabled. Every tool declares flags: `private` (its
results may not leave the box), `confirm` (asks the human first),
`read_only`, `poll_safe`. The flags are enforced by the loop, not by the
model's goodwill.

The run's toolset is chosen **once** at run start and frozen — a stable tool
schema keeps the prompt cache warm. In the shipped `auto` mode, ~17 core
tools always ship; everything else loads by keyword in your message
("commit" → the whole git namespace). `tools.load` is the bounded escape
hatch when the guess was wrong (max 2 expansions per run).

### 2.2 Skills — the agent's playbooks

25 built-in SKILL.md documents (+1 from the graphify plugin). A skill is
markdown know-how: when to use it, which tools to reach for, in what order,
and where the traps are. The catalog (name + when-to-load) sits in the
system prompt; the full body loads via `skill.load` only when needed —
so skills cost nothing until used.

Skills come in three flavours:

- **Workflow playbooks** — `coding`, `deep-research`, `tdd`,
  `diagnosing-bugs`, `codebase-review`, `web-research`, `long-document`.
  They orchestrate existing tools into a discipline.
- **Method playbooks** — the `fable-*` family (classify → define done →
  evidence → act → verify → report), `grilling` (relentless clarification),
  `writing-great-skills` (meta: how to write skills).
- **Format drivers** — `pdf`, `docx`, `pptx`, `xlsx`, `archives`, `image`:
  each bundles real helper scripts next to the SKILL.md; the skill tells the
  agent how to drive them.

### 2.3 Chains — fixed pipelines

A chain is a YAML file of steps: `agent` steps (bounded sub-agents with
their own tool set and budget) and `prompt` steps (one stateless local LLM
call). Templates wire steps together (`{{input}}`, `{{steps.<id>.output}}`).
Run via `chain.run(name=…, input=…)`. Design constraint worth knowing: prompt
steps are **local-only on purpose** — a cloud call inside a chain would
bypass the privacy gate, so the engine refuses it. One chain ships
(`research-brief`); the mechanism is proven but lightly used so far.

### 2.4 Plugins — toggleable capability bundles (new in 1.1.0)

A plugin is a directory with a `plugin.yaml` manifest plus optional tools,
skills, hooks, and web routes. Disabled plugins are **never imported**; the
admin Plugins tab can toggle them because `scan()` reads manifests without
loading code. Hooks are the interesting part: plugins can inject context into
every project-bound run (`augment_project_context`), declare which tools
such a run must keep reachable (`project_tools`), and react to file changes
and project deletion — and a plugin crash inside a hook is isolated, never
takes the loop down. `graphify` is the first and (so far) only plugin.

### 2.5 Studio & the custom layer

Everything above has a shadow layer in `$JAYNET_DATA/custom/`: skills,
chains, tools and declarative API connectors the admin creates in the
browser (Studio tab), with AI-assisted drafting by a local model. Custom
wins on name clash and survives `git pull` deploys. Everything packs into
shareable `.jaypack` zips.

---

## 3. The tool families, honestly

### Coding & files — `fs.*`, `code.*`, `git.*`, `lint`, `test`, `architect`

The most finished cluster. The ladder is deliberate:

| Need | Tool | Why this one |
|---|---|---|
| Locate | `fs.find`, `code.tree` | one call instead of ten `fs.list`s |
| Understand | `code.symbols` (definitions/references), `fs.grep`, `fs.read` with line range | `path:line` handles, never whole files |
| Change | `fs.edit` (one spot), `code.patch` (multi-hunk diff), `fs.write` (new file) | smallest precise edit wins |
| Verify | `lint.run` → `test.run` → `code.run` | cheapest signal first |
| Checkpoint | `git.add/commit`, `git.stash` | gated, so nothing slips |

`code.run` runs sandboxed in the project; `ops.run` is the explicitly
UN-sandboxed, allowlisted host variant; `job.start` is for detached
long-runners with GPU access. The three are easy to confuse but the
descriptions steer correctly.

**Strength:** the `architect` + `code.delegate` pair. `architect` is a
plan-first handler that has the specialist poke holes in the plan, then
executes unit-by-unit, each unit gated on its own `| check:` command.
`code.delegate` ships the task to the dense coder model on GPU 1, keeping
the whole file/diff/test transcript out of the brain's context — and
`isolated: true` runs it in a throwaway git worktree so the live tree is
untouched until you review the diff. The verify gate even records a baseline
*before* the agent starts, so pre-existing test failures are never blamed on
the change. That detail is the difference between a coding agent you can
trust on a real repo and one you have to babysit.

### Research & web — `web.*`, `browser.*`, `arxiv.*`, `research.*`

An honest escalation ladder:

1. `web.search` — SearxNG if you self-host it, Tavily if you pay, DuckDuckGo
   HTML always. Works with zero keys.
2. `web.fetch` — direct GET + tag-strip, SSRF-guarded, with per-status
   recovery hints (a 403 tells the model to try `web.render`, not to retry
   blindly).
3. `web.render` — same page through headless Chromium, only when fetch came
   back thin.
4. `web.extract` / `web.crawl` — structured JSON from one page / across a
   paginated set.
5. `browser.screenshot` / `browser.pdf` — visual proof and archivable
   artifacts.

`research.*` is not another crawler — it's the **state spine** for deep
research: a frontier of open sub-questions, a visited set that dedups by URL,
content hash *and embedding similarity* (same facts in different words are
caught), hard depth/search budgets with a novelty-stall stop, and claims
with per-source quality scoring (.gov/.edu/arxiv high, SEO noise low). The
actual fetching is still done with the web tools; `research.*` is what turns
a fan-out into a loop that stops.

### Knowledge & memory — `memory.*`, `kg.*`, `rag.*`, `wiki`, and graphify's `graph.*`

Four surfaces, four lifetimes — this is the cluster where JayNet thinks most
clearly about division of labor:

| Surface | What it is | Lifetime |
|---|---|---|
| `memory.*` | Free-form notes/facts/decisions, FTS5-searched | cross-run, agent-maintained |
| `kg.*` | Hand-built entity→relation graph (typed nodes, JSON attrs) | cross-run, when *structure* matters |
| `rag.*` | Embedded chunks of your documents, cosine search (brute force in numpy — fine to tens of thousands of chunks) | cross-run, read-side retrieval |
| `wiki` (skill) | Interlinked markdown pages the agent maintains | cross-run, *compiled synthesis* |
| `graph.*` (graphify) | Auto-built map of ONE project's code + docs | per project, rebuilt on demand |

The `wiki` skill states the doctrine itself: memory holds small facts, RAG
holds raw sources, the wiki holds the synthesis. `kg.*` and `memory.*` even
share one SQLite file — one "world model" substrate, structured and unstructured
halves.

### Delegation & model power — `agent.spawn`, `code.delegate`, `llm.call`, `council.debate`, `serve.*`, `model.*`

Five ways to spend model cycles, each with a distinct job:

- `agent.spawn` — a nested sub-agent with its own context, a budget carved
  from the parent, and tools that can only be a *subset* of the parent's (no
  privilege escalation; confirmations still surface to the human).
- `code.delegate` — an opinionated wrapper over spawn: right model, right
  toolset, one call. The "thin front door" pattern shows up everywhere in
  JayNet and it's a real design smell when it's missing.
- `llm.call` — one stateless shot at a cloud model (Kimi for hard tasks,
  Qwen for cheap bulk, Gemini for second opinions, GLM for 1M context).
- `council.debate` — a panel of models argues across rounds (independent
  openings, then rebuttals), brain synthesizes. Runs the two GPUs in
  parallel; cloud panelists count against the run's cost ceiling.
- `serve.*`/`model.*` — the process manager that launches/stops
  llama-servers from the preset catalog and registers them as LiteLLM
  aliases mid-run, so "spin up an embedder for this task" is one tool call.

### Verification & evaluation — `verify.*`, `eval.*`, `trace.*`

`verify.score` is the quietly excellent one: instead of asking a judge model
for a discrete score, it reads **logprob expectations over graded tokens**
(averaged over repeats and criteria) and gets a continuous [0,1] score — no
ties, no "the judge said 7". `verify.rank` does best-of-N with it.
`eval.*` runs behavioural test cases through the *real* agent loop and grades
them — the complement to the pytest suite (which tests the harness, not the
behaviour). `trace.*` reads the run history back (`trace.query`) and mines
recurring tool-call sequences worth compiling into new tools (`trace.mine`).

### Operations, continuity & control — everything else

`schedule.*` (unattended one-shot/recurring runs), `job.*` (detached GPU
jobs), `gpu.status`, `ops.status` (is the stack even up? — asked before you
debug a dead process), `archives.*`, `deliver.files`, `pdf.create`,
`docs.summarize` (survey a document tree without blowing context).

And the continuity kit, small but load-bearing: `note.set` (run scratchpad
that survives compaction), `context.pin` (protect one tool result verbatim),
`todos` (the visible plan, rendered live in the UI side panel), `ask.user`
(batched clarifying questions), and `/goal` mode — a supervisor that chains
runs against one objective until a tool-free judge call confirms
`goal.complete` or ceilings stop it.

### Integration & extension — `chain.*`, `mcp.*`, `skill.*`, `tools.load`, plugins

`mcp.list`/`mcp.call` bridge external MCP servers (stdio or HTTP): every
call confirmation-gated by default, results private, subprocess env scrubbed
of secrets — the posture is "MCP servers are arbitrary external code", which
is correct. `skill.load` and `tools.load` are the in-run levers; plugins and
Studio are the admin-time levers.

---

## 4. Graphify — the newest piece, in depth

The graphify plugin gives **each project its own knowledge graph**: code
parsed locally via tree-sitter AST (no LLM), documents and PDFs semantically
extracted by your configured local model. Output is `graphify-out/` inside
the project dir — delete the project, the graph goes with it.

The five tools: `graph.build` (background build, poll `graph.status`),
`graph.query` (plain-language question → scoped NODE/EDGE subgraph with a
token budget), `graph.explain` (one concept + everything it touches),
`graph.path` (shortest connection between two concepts), `graph.status`.
All private — a project graph is derived from your files, so it's tainted by
construction.

What makes it more than "another tool pack" is the **integration depth**,
which is really a demonstration of what plugins are for:

- A hook injects a one-line hint into every project-bound run's context:
  *"[Knowledge graph] this project is mapped (N nodes, M edges) — prefer
  graph.query over reading whole files"*, including staleness warnings.
  A second hook (`project_tools`) force-adds the graph tools to the run's
  frozen toolset, so the hint never advertises tools the model can't call —
  the keyword selector only sees message text and has no "graph" trigger.
- File-change hooks mark the graph dirty; project deletion cancels a running
  build.
- A bundled `project-graph` skill teaches the doctrine: **query the graph
  before reading files**, then read only what the graph points at; trust
  `EXTRACTED` edges, verify `INFERRED` ones.
- Owner-scoped web routes serve the interactive viz (CSP-sandboxed) and the
  report in the console, with a graph bar in the project UI.

The honest limits: builds on big projects take a while (a token-budget knob
is the speed lever), staleness is a flag + hint rather than an automatic
rebuild, and the graph is per-project — cross-project questions aren't there
yet (`merge-graphs` is parked).

---

## 5. Where the pieces harmonize

These pairings are designed as systems, and it shows:

1. **deep-research = skill + state + tools.** The skill is the choreography,
   `research.*` is the durable state, `web.*` does the fetching, `rag.*`
   holds the corpus, `agent.spawn` fans out. Nothing in the chain is
   optional fluff; the skill's steps map 1:1 onto the tools' verbs.
2. **The coding pipeline.** `architect` plans → `code.delegate` executes per
   unit in a worktree → repo-map + JAYNET.md orient every child → verify
   gate compares against a pre-run baseline → git tools merge or discard.
   Skills `coding`/`coding-projects`/`tdd`/`diagnosing-bugs` sit on top and
   pick the right lane by task size.
3. **Knowledge surfaces with a treaty.** memory/kg/rag/wiki each have a
   stated jurisdiction; the wiki skill even polices the borders ("cite the
   source, don't copy it").
4. **The privacy gate is one mechanism, applied everywhere.** Spawned
   children, council panelists, chain steps, MCP results, verifiers — every
   path that could leave the box goes through the same cloud gate. This is
   the kind of consistency you only get when one loop owns it.
5. **Continuity anchors.** Goal + note + todos are re-injected into context
   after compaction, so a long run keeps its spine while its fat gets
   trimmed.
6. **Graphify + projects.** Hooks, skill, UI and tools interlock: the graph
   knows when it's stale, the run knows the graph exists, the user sees the
   build in the console. It's the reference example for what a plugin should
   feel like.

---

## 6. Where they compete

Mostly healthy competition — same capability at different altitudes — but a
few spots where the seams show:

- **Five ways to fetch a page** (`web.fetch` / `web.render` / `web.extract`
  / `web.crawl` / `browser.*`). The descriptions define a clear escalation
  order and the fetch errors even nudge the model toward the next rung, so
  in practice this works — but it's a lot of surface for "get me this page".
- **Three depths of research**: plain `web.search`+`fetch`, the
  `web-research` skill (handful of sources, quick synthesis), the
  `deep-research` machinery, and the `research-brief` chain as a canned
  version of the middle. Well-differentiated, but a new user has to learn
  the ladder.
- **`kg.*` vs `graph.*` — two "knowledge graphs" that aren't related.**
  `kg.*` is a hand-curated, global entity store; graphify's `graph.*` is an
  auto-built, per-project map. Different jobs, colliding vocabulary — and
  the keyword selector routes "knowledge graph" to `kg.*`, not to graphify.
- **Five persistence surfaces** (`note.set`, `context.pin`, `memory.append`,
  `todos`, wiki). Again: different lifetimes (this run / verbatim /
  cross-run / visible plan / compiled knowledge) and the system prompt gives
  each one a one-liner — but it's a lot of places to "write something down".
- **Five orchestration primitives** (`agent.spawn`, `code.delegate`,
  `architect`, `chain.run`, `council.debate`). The delegate/architect
  wrappers absorb most of the choice; chains remain the odd one out —
  powerful, but with one shipped example they're more promise than habit.

---

## 7. Verdict

### What's built well and works well together

- **The loop is the product, and it shows.** Budgets, compaction, anchors,
  confirmation gates, tracing — the unglamorous machinery is where the
  quality is. Everything else is a guest in the loop, and the loop enforces
  the rules (flags, gates, tool subsets for children) mechanically, not by
  prompt persuasion.
- **The "thin opinionated front door" pattern** (`code.delegate` over spawn,
  `tools.load` over schema rebuild, `architect` over plan-execute) keeps
  guarantees in one place and gives the model simple verbs. Where JayNet
  uses this pattern, the tools feel coherent; it's the best design instinct
  in the codebase.
- **State discipline.** SQLite-per-concern, atomic writes, private flags,
  dedup by hash *and* embedding — small, local, inspectable. Nothing here
  needs a service, and nothing phones home by accident.
- **Skills as state spines + tool choreography.** The deep-research and
  coding pairs prove the model: a tool that keeps state, plus a document
  that teaches the loop, beats either alone.
- **Descriptions that tell the truth.** Checking them against the code, they
  hold up — including the warnings (what *not* to use a tool for, what costs
  money, what leaves the box). That honesty is what makes a 113-tool surface
  steerable at all.
- **Graphify's integration depth** is the right bar for plugins: not just
  tools, but hooks, a skill, UI and staleness semantics.

### What could use a little more work

- **Plugin tools aren't in the catalog.** `docs/catalog.md` (and its
  generator) predate plugins, so `graph.*` appears in no reference table —
  a discoverability hole for exactly the feature meant to be discoverable.
- **The kg/graph name collision** deserves either renaming or one sentence
  of disambiguation wherever both are mentioned.
- **Staleness is one-directional.** File edits through the *web API* mark
  the graph dirty; writes made by the agent's own `fs.*` tools inside a run
  don't (known, parked). Until that's fixed, "the graph says X but the file
  says Y" will happen after agent-heavy runs.
- **Chains are underinvested relative to their elegance** — one shipped
  example, and nothing that uses the knowledge surfaces.

### What's missing (in my opinion)

- **Automatic graph rebuilds** (dirty flag + hint is the current ceiling;
  parked in ToDos) — the difference between "the graph is a snapshot" and
  "the graph is current".
- **Cross-project graphs** (`merge-graphs`) — once you have per-project
  maps, the first question users ask is "where else do we do this?"
- **Behavioural evals for the new doctrine** — there's no eval case for
  "agent queries the graph before grepping", which is exactly the kind of
  habit `eval.*` exists to protect.
- **A bridge between knowledge surfaces** — e.g. seeding `kg.*` entities
  from graphify nodes, or letting `rag.search` see graph excerpts. Today the
  four surfaces coexist under a treaty but never talk to each other.
- **A second plugin.** One plugin proves the mechanism works; two prove the
  interface is right. The plugin system is new, and graphify was built by
  the same hands that built the host — the real API test is a plugin the
  core authors didn't write.

---

*Related: `docs/catalog.md` (full tool/skill reference),
`docs/architecture.md` (layout + subsystems), `docs/plugins.md` (plugin
authoring), `LEARNING_GUIDE.md` (theory), `handoffs/` (briefings for
extending each layer).*
