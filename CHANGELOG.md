# Changelog

Breaking changes and release notes. Versions are git tags; the stable API
contract lives in `docs/api.md`, upgrade procedure in `docs/upgrading.md`.

## 1.7.3 — 2026-09-05

**Fix (swap chain, last link).** The shipped `runtime.yaml` pinned
`tools.code.delegate.model: local-specialist` — a pinned alias
short-circuits the ENTIRE routing block in `code.delegate` (no
strength_route plan, no ModelUse, no auto-swap), which is why the swap
validation runs kept showing a perfectly armed gate with no swap behind
it. Default is now `model: null` (route by strengths: live holder → swap
→ allround → brain); the pin remains as the documented deliberate bypass.

**Docs.** llama-ops troubleshooting: the strict-template 500
(`System message must be at the beginning`) — a model template that
rejects mid-conversation system notices 500s every harness-injected turn
(stall ladder, deliverable/budget warnings) and LiteLLM's fallback then
silently serves those turns from the brain instead of the specialist.
The entry covers the symptom, the silent-fallback diagnosis, and the
one-line template-patch fix. Plus the audit #14 note on the retroactive
v1.7.0 tag's version string.

## 1.7.2 — 2026-09-04

**Strength routing that actually swaps.** Three validation runs over the
same security cluster peeled the chain layer by layer; every layer is now
enforced by the harness instead of asked of the model:

- **Auto-swap in `code.delegate`** — the routing plan is live holder →
  swap a stopped LOCAL tagged preset onto its slot → allround only when no
  preset carries the tag (remote presets never swapped). Security work now
  stops the coder on the slot and loads the security model instead of
  settling for the allround specialist. Post-swap confirm probe; honest
  note on fallback.
- **Strength injection** — live evidence: the gate armed but the brain
  dropped `strength=` on 4/4 delegate calls, silently routing coding. The
  loop now fills an omitted `strength=` with the armed gate's tag; an
  explicit one always wins.
- **Manager-aware swap stop** — live evidence: swaps still failed because
  `model.use` only stops serve-registered servers while the Processes-tab
  slots are boot-posture managed. `swap=true` now stops them through the
  process manager (`stop_one` disarms auto-restart — a raw kill would have
  been resurrected mid-swap to fight for the port).
- **Delegate toolset guard** — child tool-sets without a mutation tool are
  rejected (live evidence: the brain passed `tools=["lint.run"]`, the
  child returned nothing, the parent wrote inline).
- **Deliverable early warning** — one-shot reminder at 75% of the iteration
  budget when task-named files are still missing (`warn_at`, 0 disables);
  the final-answer check was coming too late for runs that spend their
  last turns still computing.
- **Procedure library step 2** — `debug-and-fix` (reproduce-first) and
  `research-and-verify` (two-source cross-check) join
  `implement-from-spec`, each distilled from its observed eval failure
  cluster; selector keywords in defaults and shipped config.

Also: `code.delegate` accepts `strength=` explicitly; the live-slot probe
cache is invalidated on serve/stop (was up to 120 s stale after swaps);
j-space-loop passed an eval for the first time. Suite 1552 → 1571.

## 1.7.1 — 2026-09-04

**Docs housekeeping (audit #13).** `docs/catalog.md` regenerated so
`implement-from-spec` (and an updated `fs.write` description) appear; the
new admin **Usage** tab is documented in `docs/admin.md` and added to the
screenshot sweep's tab list. No behavior changes.

## 1.7.0 — 2026-09-04

*Tag anomaly (audit #14): v1.7.0 was cut retroactively on the feature tip
without a release commit, so a checkout at this tag still self-reports
`__version__ = "1.6.1"`. The code is the 1.7.0 feature set; the version
string only caught up in the v1.7.1 release commit.*

**From asking to enforcing.** Four new loop mechanisms turn the 1.6.x
routing doctrine into machinery, each config-gated and one-shot where it
should be:

- **Stall ladder** — three escalating one-shot directives on consecutive
  no-progress turns (2/4/6); any mutation resets the counter, poll-only
  turns are neutral. Breaks the frozen-brain pattern.
- **Strength gate** — with a live strength-domain holder (e.g. `security`),
  inline `fs.write` / `fs.edit` / `code.patch` are rejected until the first
  `code.delegate`; single-model installs are untouched.
- **Procedure library v0 + auto-selector** — `implement-from-spec`, the
  first `shape:`-tagged skill, loads just-in-time at run start on a
  confident keyword match (benchmark-style "implement X from spec" tasks).
- **Badge watch** — skills flagged `requires_badge` get a one-shot
  `run.badge` reminder on the first file edit, so the badge stops relying
  on small models volunteering.

Plus: history **sanitize for invalid-JSON tool args** (llama-server 500 on
history parse) and the admin **Usage tab** — per-tool / per-skill call
counts and last-used from the trace log. Suite 1533 → 1552.

## 1.6.1 — 2026-09-02

**Fix (audit #12 D2).** Shipped `runtime.yaml` `routing_nudge` keyword lists
synced with the code fallback — they're a pure override, so live installs
silently missed the 1.6.0 intrusion/incident-response and `shell script`
nudges. `test_shipped_config_keywords_cover_the_fallback` guards the drift.

## 1.6.0 — 2026-09-02

**Harness doctrine, from two full eval runs.** "Route, don't do" is now
enforced mechanically, not just prompted: a per-run routing nudge fires
right before the user turn on coding/strength keywords (security cases
get told which preset to `model.use`), inline edits get a delegate gate,
and `code.delegate` children get a coding-sized default budget (24 iters)
instead of the fleet default 8. Gate prompt states the doctrine plainly.

**Feature — deliverable check.** The top tb failure mode (~half of all
failures) was the agent solving the task and never calling `fs.write`.
At the final answer, files the task or answer *named* but that don't
exist in the workspace now bounce the answer back once ("create it now")
instead of being accepted. `agent.deliverable_check.enabled`, default on.

**Feature — Run delta.** Admin → Eval gets a second bulk run beside
**Run all**: cases that passed their last 3 runs are skipped, except a
random 10% spot-check so silent regressions still surface. Explicit
selections and scheduled suites always play what they named.

**Feature — wall-clock liveness extensions.** A run that hits its
wall-clock cap mid-work gets short grace extensions (eval default
120 s × 5, chats off) as long as it keeps answering the iteration-boundary
"ping" — zombie runs still die, slow finishers keep their work.

**Fixes.**

- Streaming model turns get the same malformed-tool-call-JSON-500 nudged
  retry as non-streaming (a giant `fs.write` no longer kills chat runs);
  the non-streaming retry no longer recurses inside the model semaphore
  (latent deadlock at concurrency limit 1).
- Routing-nudge security keywords cover intrusion/incident-response
  phrasing; short acronyms (`rce`/`cve`/`xss`) match on word boundaries —
  "source code" no longer triggers a spurious security nudge.
- `fs.write` description tells the model to chunk large files.
- Fictional-root fs rebase: `/app/...` paths from container task
  statements resolve onto the work_root instead of hard-failing.
- Eval robustness: crash rows persist, big seeds go via stdin, userns
  work_root scrub, backend-outage grace probes before suite abort, and
  the test suite can no longer touch real systemd.
- Benchlab grades tb container cases via the task's own `run-tests.sh`.
- Devbox idle reaper actually runs; mobile header gets new-chat/save
  buttons out of the ⋮ menu with uniform popover icons.

**Breaking (tool surface).** `code.execute` and `code.run` merged into ONE
execution tool. Every eval run this month needed a `code.*` fix; the two
verbs differed by intent, not capability, and small brains kept picking
the wrong one (2026-08-29 tb cluster: agents wrote deliverables via
code.run into the devbox where `/app` doesn't exist — solved tasks, lost
files).

- **`code.run` is now the one verb**: `command` + `language: bash|python`
  (bash default for the dev loop, python for snippets with the
  ORCH_EXEC_OUT/WORK channels, matplotlib Agg, and the `llm_query`
  subcall seam). The HARNESS picks the backend — eval case container →
  devbox → host firejail — the model never chooses a filesystem.
- **Container mode covers both languages.** In Terminal-Bench container
  cases code.run now execs inside the task container too (previously only
  code.execute did) — absolute `/app/...` paths work from either verb.
  Case containers default to network ON (official TB posture; opt out per
  case with `container.network: false`).
- **`code.execute` stays as a visible legacy alias** (maps `code`→
  `command`, defaults to python) so saved chats, imported eval cases and
  older skills keep working. New prompts/skills say code.run.
- **Unified result contract**: status is `error` only when the tool
  itself couldn't run; a non-zero exit is `ok` + payload
  (`ok`/`exit_code`) — a failing test is not a tool error.
- **Eval-suite outage brake**: a run failing with backend ConnectError
  now aborts the suite ("suite aborted: model backend unreachable")
  instead of burning the whole queue in seconds — the 2026-08-29 crash
  poisoned 74 cases in 1.4 s.
- Python mode runs in the devbox too (heredoc `python3 -`, EXEC_OUT
  artifacts land back on the host); gate prompt, 7 skills, sub-agent
  lists (web.crawl/web.extract), benchlab importer wording, catalog and
  docs updated.

**Feature.** Tool-description overrides get an admin surface (Admin →
Tools → Description overrides): list, add/update and delete for the
`tool-overrides.yaml` apply-target of accepted eval proposals. Deleting
restores the shipped description live (pristine text is stashed at apply
time); entries for removed tools are flagged "unknown tool" for pruning.
Operator note: the stale `code.execute` override on existing installs
shows up there — one click restores the new alias description.

**Audit-#11 closure (D1–D4).**

- **D1 — the glm provider pin reaches the proxy.** `cloud_store.render`
  now merges seed-only `litellm_params` (extra_body provider order,
  thinking, …) into rendered cloud entries by alias — the OpenRouter
  upstream pin was dead config on every rendering install (the ~22%
  fallback-judge fix never actually shipped). DB columns win on overlap.
- **D2 — alias gating regression fixed.** `code.execute` normalizes its
  args before `needs_confirmation`, so a disabled python sandbox
  (`tools.code.sandbox: null`) gates bare host python again — the
  pre-merge "bare execution is never silent" doctrine.
- **D3 — `tools.code.timeout_s` is live again** as the python-mode
  default timeout (bash keeps `tools.code.run.timeout_s`).
- **D4 — the docx skill names its network gate** (`network: true` needs
  `tools.code.run.allow_network`, off by default) with a `job.start`
  fallback.

## 1.5.2 — 2026-08-29

**Audit-#10 closure (D1–D3; D4 screenshots pending a live re-shoot).**

- **Connector SSRF guard now resolves DNS** (D2). The construction-time
  check only caught link-local IP literals — a pack-supplied hostname
  resolving to 169.254.x slipped through. The request path now resolves
  the host and refuses if ANY address is link-local (IPv4-mapped IPv6
  unwrapped), mirroring the web tools' posture with the connector policy
  (loopback/RFC1918 stay allowed for homelab targets).
- **`| check:` trust surface named + bounded** (D1). `docs/security.md`
  documents the unattended execution of a goal's check command beside the
  schedule auto-confirm entry, and `goal.check_timeout_s` floors to the
  120 s default on 0/unset — there is no unbounded mode.
- Playbook coverage line caught up to its own body (v1.5.1) (D3).

## 1.5.1 — 2026-08-28

**Feature.** `/loop` — the fresh-context objective loop (the "Ralph"
pattern). Same grammar and supervision as `/goal` (`| done when:`, pause /
resume / stop, ceilings, completion judge), but every iteration launches
with an EMPTY context window: no accumulated history to degrade, no
compaction drift — the workspace files are the loop's only memory. The
harness carries the state spine deterministically: STATE.md written by one
iteration is captured and injected into the next continuation, so even
small models can't lose the plot. Turns publish as 🔄 in the chat. Use
`/loop` for long marathons on smaller models, `/goal` when conversation
context matters.

- **Deterministic completion: `| check: <cmd>`.** Optional on both `/loop`
  and `/goal` (either order in the grammar): on every completion
  declaration the command runs in the goal's workspace — exit 0 finishes,
  anything else logs the output and feeds it into the next iteration.
  A check replaces the judge (deterministic outranks model opinion);
  `goal.check_timeout_s` (default 120) bounds it.
- **Guided start.** The compass button next to *new chat* asks "what would
  you like to achieve?" plus two short questions and routes to the right
  tool — plain chat, `/loop`, `/goal`, or a new project — prefilling the
  composer (never auto-sending). Choosing the right tool is no longer the
  hard part.

**Feature: connector packages.** Connectors grow up from single declarative
HTTP tools into shareable SYSTEM packages — one connector = one external
system (Gmail, the LAN mail server, an ERP) exposing a namespace of tools.

- **Data, not code** — the deliberate line to plugins (which extend JayNet
  itself): a connector pack is interpreted YAML, so importing one can never
  execute anything. Secrets are env-var NAMES; per-box settings, enable
  state and mode live in `custom/connectors.json`, never in the pack — a
  `.jayconn` is safe to share by construction.
- **Package format**: `<id>/connector.yaml` with a `tools:` list, a
  `settings:` schema (auto-rendered admin form), package-level
  base_url/auth defaults, `{settings.KEY}` interpolation, and an
  `allows: ro|rw` ceiling. Legacy single-tool files keep working unchanged.
- **Enable/disable and read-only/read-write per connector, hot** (no
  restart): disabled removes the tools; RO drops write tools entirely
  (absent, not gated — an explicit `write: false` marks idempotent POSTs
  that survive). New packages with writes START read-only; an import must
  be deliberately promoted.
- **Admin → Connectors tab**: toggles, settings forms, a test probe (first
  read tool, never a write), `.jayconn` export, delete, README viewer, load
  errors surfaced. jaypack imports/exports the package shape.
- **SSRF guard**: connector base_urls pointing at link-local/cloud-metadata
  addresses (169.254.x & co.) are rejected unless the pack explicitly opts
  in — RFC1918/loopback stay allowed, homelabs live there.
- Authoring + sharing guide: `handoffs/connectors.md`.

## 1.5.0 — 2026-08-28

**Fix + feature.** A hardening round driven by a 124-case Terminal-Bench
full-mode post-mortem and a full-suite proposal triage: eval run-killers
fixed, the judge reined in, the harness learns from in-chat corrections,
and GAIA joins the benchmark roster.

- **Fixed: rendered LiteLLM config no longer shortens the seed timeouts.**
  `cloud_store.render()` hardcoded `timeout`/`request_timeout` 120 while the
  seed `config/litellm.yaml` had been raised to 600 — the proxy killed every
  local thinking turn over 120 s with a 408, ending eval runs mid-answer.
  The renderer now takes both values from the seed (600/600 fallback).
- **Terminal-Bench full mode grades what upstream grades.** The task image
  build is split into base + a thin test layer (`…-t<hash>`): pytest AND the
  tests' own pip deps (scanned from test imports, with an import→package
  map) install at import time — tasks whose checks import cv2/pandas/psutil
  & co. were unpassable before. Staged tests now land at `/tests` in the
  container (the upstream convention tasks reference).
- **Container cases can have network.** New `container.network: true` case
  field drops `--network none` for that case (default stays air-gapped).
  benchlab sets it for tb-full — official Terminal-Bench allows downloads,
  and the container is throwaway and credential-free.
- **Per-case wall-clock override.** New case-level
  `budget: {turn_wall_clock_s: N}` wins over `eval.turn_wall_clock_s`;
  benchlab stamps 1200 s on tb-full cases so one marathon task can't eat a
  suite's whole evening.
- Full-mode instruction now tells the agent that host `fs.*` tools see /app
  as the project root (relative paths) — absolute /app/... is terminal-only,
  which was losing output files.
- **Eval tab ergonomics.** Results moved into their own sub-tab; cases can
  be **deactivated** (state in eval.db — works for built-ins, survives
  re-imports): disabled cases drop out of run-all/tag/scheduled runs but
  stay runnable explicitly, for "my brain can't pass this yet" cases.
  Checkboxes run an arbitrary multi-selection (`ids` in the run API), and
  custom cases delete straight from the row.
- **Reflect: the harness now learns from in-chat corrections.** Explicit
  corrections in *successful* sessions ("no, use uv instead of pip") used
  to die with the conversation — the flag/eval improvement loop never saw
  them. A detached post-run watcher (`runtime/reflect.py`, config
  `reflect.*`) gates on correction phrasing, lets the LOCAL brain judge
  whether it's a generalizable teaching (chat content never leaves the
  box), and files a dedup'd proposal — targeting the skill actually loaded
  in that session, hallucinated skill names downgrade to prompt-tweak.
  Same supervision bar as eval proposals: nothing auto-applies.
- **Fixed: benchlab test-layer builds no longer die on one bad dep.** The
  deps scanner skipped neither tests-dir helper modules (fit_model.py & co.)
  nor the agent's own solution module, so `pip install` failed the whole
  layer and the task kept its old, unpassable case (16 tasks on a live
  import). Helpers are now scanned out; extra deps install per-package
  tolerant (pytest stays strict) — a genuinely missing dep still fails
  loudly at grade time. Layer recipe bumped (v3), so the next import
  rebuilds the layers.
- **Judge stops proposing operator settings.** The judge's state block now
  shows the case's own `case_budget` and its rules forbid global budget,
  wall-clock, timeout, proxy, or network proposals (a live suite produced
  dozens of unactionable — and dangerous-to-accept — `budgets.*` proposals
  for per-case marathons). Config proposals are restricted to the
  whitelisted behavioural knobs; anything else is bug-for-dev or bad-test.
- **Proposal inbox: one open item per (case, class).** Exact-hash dedup
  let the judge's paraphrases pile up (six near-identical "strengthen the
  delegation directive" rows for one case); a fresh proposal now replaces
  older still-pending siblings for the same case+classification.
- **Gate prompt: execution-discipline directives**, consolidated from a
  full-suite's worth of accepted eval proposals: deliver named output files
  (create + verify, chat is not a deliverable), delegation sharpened to
  implementations-that-land-in-files, batch independent shell commands,
  cross-check exact counts, state the current year in freshness answers,
  explicit memory recall, `council.vote` for high-stakes single answers,
  no permission-asking when the deliverable is clear. The shipped prompt
  also gains the "Named skill? Load it." and "Trace transitive impact."
  bullets that only existed in live overlays.
- **Fixed: malformed-tool-call 500s no longer kill runs.** llama.cpp
  parses tool-call arguments server-side and answers HTTP 500 when the
  model mangles a long JSON argument (multi-KB `fs.write` payloads) — the
  whole run died mid-flight (5/118 cases in one live suite). The brain
  call path now retries the turn once with a nudge (keep arguments small),
  and the gate prompt advises writing large files as several smaller
  `fs.write` calls. Non-streaming path (eval, sub-agents); the streaming
  chat path is untouched.
- **Fixed: GAIA import skipped every row.** HF's schema spells the gold
  field `"Final answer"` (space); the importer read `"Final_answer"`
  (underscore), so every row failed the required-fields check. Both
  spellings are accepted now (verified against the live gated dataset:
  10/10 rows build).

## 1.4.0 — 2026-08-26

**Feature.** The harness starts enforcing its own doctrine (delegation,
strategy change on crash loops, reasoning budgets), execution grows a real
toolchain container, and the eval loop gets the forensics a live full-suite
run demanded. No breaking changes:

- **Devbox: `code.run` can compile the world, not just the host.** The
  firejail sandbox only has what the host has installed — "write me Rust"
  produced code the harness couldn't compile. New opt-in
  `tools.code.devbox`: build the toolchain image once
  (`scripts/devbox-build.sh` → `containers/devbox/Containerfile`: rust,
  go, node, C/C++, java, python, **.NET 8 + 10** — Ubuntu 26.04 base, the
  only distro packaging .NET first-party) and `code.run` executes inside a
  per-run rootless podman container instead of firejail. Same confinement
  shape (only the run's workspace + tmp mounted), cargo/go/npm/nuget
  caches on shared volumes so iterative builds stay fast, idle containers
  reaped, network on for registries but ALWAYS cut on private-tainted
  runs (a live `network disconnect` when the taint arrives mid-run).
  Podman or image missing → silent fall back to the classic sandbox with
  a note; nothing changes for existing installs until enabled.
- **`local_concurrency` defaults raised 1 → 4 for brain and specialist.**
  Current llama.cpp builds default to `n_slots=4` with a unified KV cache,
  so the client-side cap now matches the server out of the box: fan-out
  children, parallel tool turns, and eval/bench runs genuinely overlap
  instead of queuing behind one slot. It's a cap, not a multiplier —
  single-chat runs still fire one call at a time. Embed/rerank servers are
  unaffected (RAG calls them directly and batches internally). On older or
  explicitly single-slot servers, lower the alias back to its `-np`.
- **`council.vote` — self-consistency voting for small local models.** Ask
  one model the same single-answer question N times in parallel at
  temperature and majority-vote the extracted `ANSWER:` lines. Any one
  sample from a small MoE may be wrong; the correct answer is usually the
  mode. Returns the winner, vote distribution, and per-sample previews;
  failed samples abstain, ties are reported not picked. Every sample is
  charged to the run budget, and the cloud gate mirrors `council.debate`.
  From Gulli's *Agentic Design Patterns* (Ch. 17, reasoning techniques) —
  covered by the new `council-vote` eval case.
- **`agent.fanout` — map/merge parallelization.** Fan several independent
  subtasks out as concurrent sub-agents (one `ctx.spawn` each, all of
  spawn's guarantees) and get every distilled report back together — the
  reduce step is the brain merging envelopes instead of transcripts.
  Partial failure is signal, not a tool error; all-failed is. Honest
  physics in the description: same-model children serialize on one GPU
  (context isolation either way, wall-clock only across models). From
  *Agentic Design Patterns* (Ch. 3) — covered by the new `agent-fanout`
  eval case.
- **Delegate gate: the harness now insists on delegation, not just suggests
  it.** Live eval evidence: the brain implemented non-trivial coding inline
  (17 `fs.write`/`fs.edit` calls, 0 delegations) though its prompt and the
  complexity gate both say to delegate — small MoE brains don't do it on
  their own. New `loop_guard.delegate_nudge_after` (default 3): after that
  many successful inline write/edit calls with delegation available but
  unused, the tool result carries a delegate-to-the-specialist directive.
  With `loop_guard.delegate_enforce: true`, inline edits are rejected from
  the threshold on until the brain delegates — `1` + enforce = delegate
  first, literally (any `code.delegate` call disarms the gate, so
  verify-fix loops stay legitimate). The gate only engages when delegation
  would actually route somewhere stronger — a configured coder alias or a
  live coding-strength specialist — so single-model installs and runs
  without `code.delegate` are never touched.
- **The judge sees the complete tool list, not the truncated trajectory.**
  The trajectory display keeps only the most recent 14 tool entries, so a
  `skill.load` in iteration 1 of a long run was invisible at grading time —
  `skill-load` and `j-space-loop` failed on a phantom "skill never loaded"
  while the trace proved the load happened. Judge input now carries a
  per-turn "tools called (complete)" line plus trace-derived skill names.
- **Eval hardening from a live full-suite run.** Five cases were lost to
  "judge returned unparseable JSON" — OpenRouter autoroutes glm-5.2 across
  upstream providers and some return HTTP-200 garbage for `json_object`
  calls, which never raises, so the alias fallback couldn't fire and the
  retry stayed pinned to the same bad route. The judge now tries the
  fallback alias (`local-specialist`) explicitly after a failed retry and
  records the head of the offending content in the result row, so the next
  bad verdict is diagnosable from the admin UI.
- **A generation cut at the completion cap during reasoning no longer ends
  a run with an empty answer.** Found live: `tb-regex-log` returned 8192
  completion tokens of pure thinking, zero content, status "ok".
  `finish_reason` is now plumbed through both model-turn paths, and an
  empty-content-at-cap turn gets ONE brief-reply nudge before the run may
  end. Complementing this, presets gain **`REASONING_BUDGET`**
  (`--reasoning-budget`, shipped as 4096 on the brain presets): llama.cpp
  force-closes the think block at the budget, reserving answer room inside
  `orchestrator.sampling.max_tokens` — cap the thinking, not the reply.
- **Naming a skill in chat pins its load mechanically.** "Use the j-space
  skill" now injects a force-load directive into the run (same enforcement
  philosophy as `/charter` and `/wgs`) — the brain had skipped `skill.load`
  even with an explicit user instruction AND the prompt directive live.
  Conservative matching (`"<name> skill"` / `"skill <name>"`), so plain
  mentions stay untouched.
- **`delegate-strength-routing` case fixed** — the original 20-primes task
  was trivial enough that skipping delegation was arguably *correct* per the
  "Delegate coding" directive. The case now uses an unambiguously
  non-trivial task (CLI + persistence + tests), and its checker runs pytest
  before exercising the CLI on a clean state.
- **Gate prompt diet** — cut what the model can't act on: the admin-facing
  plugin enumeration (`graph.* from graphify, bench.* from benchlab` → one
  generic "plugins add namespaces, tools.load them" clause — plugin
  discovery happens via the project-context hooks, not the prompt), the
  changelog-speak in the `web.fetch` line, and the `/charter` + `/llmwiki`
  mention (user-typed commands whose routes force-load the skill and inject
  their own directive — the model never discovers them from the prompt).
- **LiteLLM request timeout 120 s → 600 s.** Long local thinking generations
  blew past 120 s and LiteLLM answered 408, killing the run mid-answer
  (found live on `tb-huarong-dao-solver`; eval proposal #25). Both
  `router_settings.timeout` and `litellm_settings.request_timeout` in
  `config/litellm.yaml` — pull + restart litellm to apply.
- **New eval case `delegate-strength-routing`** — the first deterministic
  model-switching test: `must_use_tools: [code.delegate]` hard-fails a run
  where the brain wrote the code inline (the older `delegate-coding` case
  deliberately allows verified local execution), and a sandbox checker
  verifies the delegated script actually prints the first 20 primes. It
  declares `requires_tools: [code.delegate]`, so it **skips cleanly** under
  `brain` benchmark variants (which strip the delegation verbs by design)
  and runs hard under `full`/default — the same suite is now a valid A/B for
  both harnesses. The Benchmark docs also gained the missing `harness:
  full|brain` variant field description.

- **`web.fetch` extracts main content via trafilatura** — nav/footer/sidebar/
  cookie boilerplate no longer fills the 50k content cap (and the model's
  context) on every fetched page; the plain tag-strip stays as fallback when
  no main content is found, trafilatura is disabled
  (`tools.web.trafilatura_enabled: false`) or not installed. Replaces the
  Tavily `/extract` role — same quality tier, but local, free, and your URLs
  stop leaving the box. Tavily remains as a search backend. New core dep:
  `trafilatura` (requirements.txt — `uv pip install -r requirements.txt` on
  upgrade).
- **Eval UI**: case rows are click-to-select (the "Run selected case" button
  previously had no way to select), and a confirmed **Run all** button runs
  the whole library via the new `all` flag on `POST /api/admin/evals/run`.
- graphify ships a Plugins-tab README (same pattern as benchlab).
- **uv is the env manager, and the harness says so**: the workspace prompt
  now teaches that venvs have no pip inside (never `.venv/bin/pip` — use
  `code.deps` or `uv pip install --python …`), and benchlab's grading
  checker bootstraps pytest with uv first (pip fallback). The checker's
  cached venv is also re-verified by import and repaired when broken —
  previously a venv whose first pytest install failed stayed broken and
  failed every later case. **pytest is now a core dep** (requirements.txt +
  regenerated locks): grading works out of the box on fresh installs and
  `code.execute` snippets can use it too.
- **Eval case runs get a wall clock** (`eval.turn_wall_clock_s`, default
  1800; 0 = unlimited). Found live: `tb-huarong-dao-solver` looped for over
  an hour rebuilding a segfaulting solver — with a local brain the $ cap
  can't fire (cost $0.00), and eval runs deliberately disable the iteration/
  wall-clock ceilings, so nothing stopped the case and the whole suite
  blocked behind it. The $ budgets stay primary; the wall clock is the
  safety net for zero-cost stuck runs.
- **Crash-retry loops get an escalation nudge** (`loop_guard.failure_nudge_after`,
  default 3). Execution tools report failures in their *payload* (`ok:false` /
  `exit_code!=0`), so the duplicate-call guards never see the classic
  "rebuild the same segfaulting solver 70×" loop. Consecutive failures with
  the same error signature (tool + exit code + normalized stderr tail) now
  get a strategy-change hint appended to the tool result — switch approach,
  and `code.delegate` to the specialist when it's in the run's toolset.
  Success or a different error resets the count; `0` disables; the watched
  tools are configurable (`failure_nudge_tools`, default code.run +
  code.execute). This is also how smaller brains learn to route heavy
  implementation to the specialist mid-run instead of grinding alone.
- **Model priority by preset strengths**: `code.delegate` without an
  explicit model now routes coding work to the LIVE specialist slot whose
  preset carries the `coding` strength tag (`allround` counts as
  coding-capable; exact tag beats allround across specialist → specialist2
  → specialist3), falling back to the default brain with the honest note
  only when nothing coding-strong is live. The pinned alias
  (`tools.code.delegate.model`) still wins when set — set it null to always
  follow strengths; the routed tag is configurable
  (`tools.code.delegate.strength`).
- **Benchmark variants get a harness dimension**: each variant now picks
  *full* (whole toolset, delegation included — JayNet's routing story) or
  *brain only* (strips code.delegate / architect / agent.spawn), so the
  Benchmark tab can A/B exactly what model routing buys. Cases requiring a
  stripped tool skip instead of failing; the variant table gained a Harness
  column and the choice is validated + carried into run_case.
- **Strength tags become a registry with meaning** (`models.strengths`):
  each tag gets a one-line description, the system prompt gains a Strength
  tags directory — `coding = code synthesis, debugging (live:
  local-specialist) · security = … (not live)` — so the brain learns both
  what to ask for and who currently provides it, and admin → Presets shows
  the known tags with descriptions + carriers under the strengths input.
  Tags on presets stay free-form; registered ones are what delegation
  routes by.
- **`agent.spawn` routes by capability tag**: the new `strength` argument
  ("coding", "security", …) resolves through the shared
  `catalog.route_strength` (exact tag beats allround, slot priority) — the
  brain names the capability, the harness tracks which model provides it.
  A tagged-but-stopped preset ("dolphin IS tagged security") returns an
  actionable error pointing at model.ensure; an unknown tag lists the
  registered ones. Explicit `model` still wins; `code.delegate` uses the
  same shared resolver.
- **Shipped gate prompt rewritten** for the current brain/specialist
  (Qwen 3.6 MoE / Qwen 3.8 27B): plugin namespaces in the tools table, a
  web & knowledge section (trafilatura `web.fetch`, graph/RAG/wiki bridges),
  and the accepted eval proposals folded in as directives (verbatim stdout
  over predicted output, `ask.user` as a tool call). The live overlay
  consolidates its pending tweak bullets the same way.
- skills: the office-format helper scripts are ruff-clean (import style,
  one unused variable) — whole-tree `ruff check .` now passes; CI's ruff
  step only linted `runtime web tools scripts tests` before.
- Audit #8 closures: pinned lockfiles regenerated on the 3.11 floor and now
  carry `trafilatura` (fresh pinned installs get main-content extraction
  instead of the silent tag-strip fallback); `llm.call`'s Gemini role text
  no longer pins a nonexistent "3.5 Pro" (the route is Gemini Pro); the
  `code.execute` snippet preamble no longer strips the interpreter's own
  stdlib/site dirs (uv-managed pythons live under `~/.local`, venvs may
  live under `/home` — snippets lost `json` there).
- **Eval-suite failure forensics** (a live 50%-failure run: 15 of 18 were
  infrastructure, not the agent): benchlab lite checkers no longer die with
  `No module named pytest` on fresh installs — the generated checker probes
  the runtime python first, then self-bootstraps a cached venv
  (`<data>/benchlab/checker-venv`, network once). **Re-run `bench.import`
  to regenerate existing tb-* cases with the new checker.** The eval judge's
  token budget goes 4000 → 12000 with `finish_reason` capture — reasoning
  judges (glm-5.2) truncated their JSON verdicts on long transcripts, and a
  truncation now reads "truncated at the token cap", distinct from genuinely
  unparseable output. `rlm-log-aggregate` accepts `code.run` next to
  `code.execute` — programmatic addressing was the point, not the tool.
- Audit #9 closures: the gate prompt's category table gained the missing
  **agent** row (`agent.fanout` — the keyword trigger loaded it, the table
  just didn't say so); the devbox image probe now latches success only, so
  enable-before-build picks the image up on the next call instead of after
  a web-process restart; `tools.code.devbox.env` is documented; security.md
  names the shared-cache-volume accepted risk; llama-ops states the minimum
  build for `--reasoning-budget`; `devbox.attempt` lost a dead parameter.

## 1.3.0 — 2026-08-23

**Feature.** The plugin system grows its last mile (distribution, live
toggling, admin UIs), the knowledge surfaces start talking to each other,
and new projects can be born with a charter. No breaking changes:

- **`.jayplugin` packaging**: jaypack gains the `plugin` kind (whole plugin
  dir, `__pycache__` excluded); export via the new button in Admin → Plugins
  or `/api/admin/studio/export/plugin/<name>`, install via **Install
  .jayplugin…** in the Plugins tab (same guards as `.jaypack`).
- **Plugin admin UIs**: a plugin may ship a static `ui/` dir, served
  admin-gated at `/api/admin/plugins/<name>/ui/` with an **open** button in
  the Plugins tab. Convention: plugin admin APIs register under
  `/api/admin/plugins/<name>/api/` for the same free admin gate. benchlab
  ships the reference UI (fetch/import with live job status).
- **Honest requirements**: `plugin.yaml` gains `requires_bins` (executables,
  checked via `shutil.which`, reported as "needs bin: …" — never blocking,
  unlike pip `dependencies`); the Plugins tab now also renders each plugin's
  README.md and declared deps. benchlab declares `git`/`podman`.
- **New builtin skill `plugin-authoring`**: guided plugin building for the
  agent — scaffold, manifest, tools/hooks/routes/UI, tests, packaging.
  Also published as a jaypack in the studio-packs repo.
- **Plugin hot-reload**: toggling in Admin → Plugins now applies **live** —
  tools, hooks, skills, routes and admin UIs register/unregister in-process
  (new runs only; in-flight runs keep their frozen toolset). Fresh
  `.jayplugin` installs get a **load now** button — no restart. A restart is
  only needed for newly installed pip dependencies.
- **Wiki pages as graph nodes** (graphify, default on): a deterministic
  extractor appends one node per project-wiki page (`files/wiki/`) plus
  `references` edges for `[text](page.md)` and `[[Page Name]]` links —
  appended before clustering, so wiki pages get communities and appear in
  the report/viz, and `graph.seed_kg` carries them into the kg as type
  `wiki`. Opt-out via `plugins.graphify.wiki_nodes: false`.
- **Knowledge-surface bridge** (graphify): `graph.seed_kg` seeds a project
  graph into the curated kg as `'<project>/<node>'` entities + relations
  (provenance attrs, merge-on-reseed, confirmation-gated), and project-bound
  `rag.search` now gets a `graph_excerpt` — the 1-hop project-graph
  neighborhood around its hits — via the new `rag_excerpt` hook. kg gains a
  public bulk-`seed()` entry point for exactly this.
- **Graphify auto-rebuild** (opt-in): `plugins.graphify.auto_rebuild` +
  `auto_rebuild_delay_s` (default 120). File changes (web edits AND agent
  `fs.*` writes) re-arm a per-project debounce timer; the rebuild fires
  after a quiet window, only for projects that already have a graph, skips
  when a concurrent build covered the changes, and never hot-retries an
  error. Off by default — the semantic pass is the expensive part.
- **Project charter interview**: creating a project now offers a charter
  interview (or plain start). Accepting sends `/charter`, a normal run with
  the new `project-charter` skill force-loaded and the project wiki writable:
  the agent interviews you one question at a time (grilling doctrine, with
  recommended answers) and compiles the answers into the wiki's first pages
  — `overview`, `goals`, `constraints`, `glossary`, `decisions`, catalogued
  in `index.md`. From there the wiki extractor carries the charter into the
  project graph, so later runs find it by asking the graph. `/charter` also
  works standalone in any project chat.

Also since 1.2.0: agent-side `fs.write`/`fs.edit` now fire
`on_project_file_changed` (graphify staleness covers both write paths);
`web.fetch` thin-content hint toward `web.render`; new doctrine evals
`web-fetch-lane` + `memory-vs-note`; conftest ORCH_HOME pin + default-root
write guard (CI parity); plugin UIs open inline in the Plugins tab (iframe
panel, not a new window); the admin plugin scan is briefly cached so iframe
asset hits don't re-scan per request (toggle invalidates); `.jayplugin`
install validates the inner `plugin.yaml` at upload time (parse + pack-name
match) instead of surfacing a bad manifest only after restart. Audit #7
closures: plugin `startup_hooks`/`shutdown_hooks` now RUN on hot toggles
(startup on enable, shutdown before unregister on disable) instead of only
being bookkept; the `rag_excerpt` hook caches the parsed graph.json by
(mtime, size) so project-bound `rag.search` no longer re-parses per request;
hot toggles invalidate the cached OpenAPI schema so `/docs` stays honest.

## 1.2.0 — 2026-08-23

**Feature.** The RLM pattern (Recursive Language Models,
[arxiv.org/abs/2512.24601](https://arxiv.org/abs/2512.24601)) lands natively:
context-as-variable without a second, unmediated agent loop. No breaking
changes.

- **Mediated sub-LLM calls from inside `code.execute`** — the missing
  primitive. Snippets get pre-defined `llm_query(prompt, …)` /
  `llm_query_batched(prompts, …)` helpers that reach the run's own model
  client over a per-run unix socket (filesystem transport — the sandbox's
  `--net=none` posture is untouched). Every call is mediated: per-execution
  token grants with a call cap, billed to the run's budget and live cost
  meter, logged to the trace as `subcall` events, hard-refused to non-local
  models when the run is private-tainted (a sandbox can't ask the human, so
  this fails safe like the tool privacy gate), and restricted to the run's
  own brain or local aliases otherwise. Caps first (`tools.code.subcalls`:
  64 calls/execution, 4 concurrent, 240s, 4096 output tokens, 400k prompt
  chars) — model-written code multiplying LLM calls is the risk they bound.
- **New tool `context.stage`** — move oversized text out of the conversation
  into a content-hashed workspace file and get a path back; address it
  programmatically afterwards instead of re-reading it whole.
- **`long-document` skill** now teaches the RLM route first: slice with
  `code.execute`, map subcalls over chunks, reduce yourself, verify exact
  answers programmatically; `agent.spawn` map-reduce is kept for chunks that
  need real tools.
- **Evals:** project fixtures grow `seed_code` (a snippet that generates the
  fixture at seed time — no 200KB YAML literals), and a new OOLONG-style
  case `rlm-log-aggregate` demands exact counts over a seeded 450KB log.
- **Audit closure (2026-08-22):** `context.stage` gets its selector route
  (a `context:` keyword family — "too big" / "out of context" messages load
  it without a `tools.load` detour), and stale `subcall-*.sock` files a
  killed process leaves behind are swept at boot (probe-before-delete, so
  live sockets in other processes survive).

**Upgrade:** Pull, restart. Subcalls are on by default; disable via
`tools.code.subcalls.enabled: false`.

**Feature.** Public agent benchmarks as eval cases: the eval schema grows
two deterministic grading keys, and a new opt-in `benchlab` plugin imports
Terminal-Bench and GAIA tasks. No breaking changes.

- **`expect.answer_exact_any`** — GAIA-scorer-style normalized exact match
  of the final answer (after a `FINAL ANSWER:` marker, the last line, or the
  whole answer; case/articles/punctuation/number-format insensitive).
- **`expect.checker`** — a Python grading script the harness runs after the
  last turn, inside the case sandbox (cwd = work_root, `EVAL_ANSWER` env),
  scrubbed env, 120s cap; exit 0 = pass, output tail = failure message.
- **New plugin `benchlab`** (disabled by default): `bench.fetch` clones the
  Terminal-Bench catalog, `bench.import` converts tasks into custom eval
  cases — suite runs, judge, statistics and Benchmark compare work on them
  like any other case. Two TB modes: **lite** (curated container-free
  subset, ~10 stdlib-only tasks, embedded pytest graders invisible to the
  agent) and **full** (rootless podman: per-task container images built from
  the upstream Dockerfiles, `code.execute` runs inside the container against
  the real task environment, grading by the task's own tests in-container —
  close to the official protocol). Plus GAIA Level-1 (gated; your own
  `HF_TOKEN`, exact-match grading). See [docs/plugins.md](docs/plugins.md).

**Upgrade:** Pull, restart. Nothing changes unless you enable the plugin
(Admin → Plugins).

**Feature.** `code.execute` grows up a little: a persistent per-run
workspace, bash in container runs, and spill-safe output. No breaking
changes.

- **Persistent workspace** — in runs with a work_root, snippets now chdir
  into `<work_root>/exec-work/` (env `ORCH_EXEC_WORK`): files survive
  across `code.execute` calls within the run and are visible to
  `fs.*`/`deliver.files`. Multi-step data work no longer recomputes state
  every call. Surfaced a latent sandbox bug: firejail `--read-write` binds
  under /tmp are hidden by `--private-tmp` — eval work_roots live in /tmp,
  so artifact delivery was silently broken there; binds now get a
  `--whitelist` companion on /tmp paths.
- **`language: "bash"`** — in container (benchmark) runs the snippet can
  run via bash instead of Python, matching how CLI-native tasks are
  actually solved; rejected outside container mode (`code.run` is the
  shell tool there).
- **Truncation spills to a file** — stdout/stderr past the inline caps are
  written to the artifact dir in full and the path returned, instead of
  silently dropping output.

**Upgrade:** Pull, restart, done.

## 1.1.6 — 2026-08-22

**Patch.** Docs-only: the README's references table now credits
Graphify-Labs/graphify (the Apache-2.0 engine behind the graphify plugin's
`graph.*` tools) alongside the j-space suite. No code changes.

**Upgrade:** Pull, restart, done — or skip it; nothing runtime-relevant.

## 1.1.5 — 2026-08-21

**Patch.** The j-space skill ships: deliberate-workspace doctrine as an
on-demand skill, a `run.badge` tool so skills can show their active mode
live in chat, two new eval cases guarding the doctrine, and a
complexity-gate nudge toward it. No breaking changes.

- **New skill: j-space** — an adapted vendoring of the Apache-2.0 J-Space
  Cognition Suite V3.6 (prompt doctrine, NOT the interpretability research
  it borrows vocabulary from): the brain classifies a task fast/full/loop,
  loads only the module the task earns, and keeps a `.jspace/WORKSPACE.md`
  ledger of settled/open/next for long work. Its plan stays in the harness
  todo list (the upstream design is explicit that the ledger is *not* a
  task list), pinned via `context.pin` for compaction survival. Modules
  and references ship verbatim; `LICENSE`/`THIRD_PARTY_NOTICES.md`/
  `NOTICE` ride along.
- **New core tool: `run.badge`** — a short live status label on a run
  (footer line + debug view, replayed with saved chats). Skills use it to
  show which mode is active; j-space badges `j-space: full` / `j-space:
  loop` at the gate and on every pass change. Registered in the core
  toolset incl. the trivial-message minimal set, so a skill loaded
  mid-run can always badge.
- **Evals:** two new cases — `j-space-loop` (multi-file rename driven
  through the loop pass: plan-before-edit, badge, tests actually run) and
  `j-space-floor` (a "quick, no ceremony" request that isn't fast must be
  escalated, not answered from the request alone).
- **Complexity gate nudges toward j-space at 3+.** Deliberately a nudge,
  not an auto-load: the skill's gate only works when the model classifies
  the task itself, and its own doctrine forbids loading machinery the task
  didn't earn. Default-on was rejected for the same reason — it would tax
  the fast path and dilute the gate prompt.
- **Audit closure (2026-08-21):** root `THIRD_PARTY_NOTICES.md` lists the
  j-space vendoring (Apache-2.0 — "all are MIT" was no longer accurate),
  release notes for v1.1.3/v1.1.4 backfilled, and the eval graph prebuild
  subprocess env now goes through `scrub_env`, same posture as the MCP
  stdio bridge.

**Upgrade:** Pull, restart, done. j-space costs nothing until loaded —
say "use the j-space skill" on a hard task, or let a 3+ complexity rating
nudge it.


## 1.1.4 — 2026-08-21

**Patch.** Two real boot fixes found by the first live plugin eval run,
the eval harness learning to mirror web project context, and one-click
consolidation of eval prompt tweaks. No breaking changes.

- **Fix: plugin toggles never took effect.** Admin-persisted config
  overrides were applied *after* the runtime loaded plugins from the
  YAML-only config, so enabling a plugin in Admin → Plugins + restart
  registered no tools/hooks/routes while the Plugins tab reported
  "loaded" (live-confirmed with graphify). Overrides now merge before
  plugin discovery, with the users DB located via `load_config` so
  relative paths (`users_db: users.db`) anchor at the data dir exactly
  like the runtime's own resolution. As a side effect, `web.*`
  overrides (e.g. `web.cookie_secure`) actually reach the web config
  now.
- **Prompt tab: one-click consolidation of eval tweak bullets.**
  Accepted prompt-tweak proposals collect as dated bullets under an
  `<!-- eval-proposals -->` marker (capped at 5, then manual merge was
  required). New **Consolidate eval tweaks** button drafts a merged
  prompt with the eval judge model (bullets folded into the prose,
  marker dropped), shows it in the source editor for review, and
  **Apply consolidation** writes a timestamped backup next to the
  overlay before saving. Deliberately NO prompt-per-model versioning:
  the gate prompt is harness doctrine, not model tuning — the eval
  suite itself is the regression guard when the brain changes.
- **Eval: project-fixture cases get the web's project context.** Turn 1
  of a `project:` case now carries the same prefix the web layer
  prepends on project-bound runs — `[Project:]` banner, file tree, and
  plugin hints via the `augment_project_context` hook (graphify's
  "[Project graph] … prefer graph.query"). Without it the agent had
  graph tools but zero nudge: the first live `graph-orientation` run
  answered correctly via `fs.read` and judge-failed the rubric
  (score 3, "undiscoverable"). The `graph-orientation` rubric was also
  sharpened to grade the runtime-vs-source-edit distinction explicitly —
  the case is green on live (10/10, graph-only navigation).

Upgrade: pull, restart, done. If you enabled a plugin in Admin →
Plugins before this release and wondered why nothing happened: this
fixes it — the toggle takes effect with the restart.

## 1.1.3 — 2026-08-21

**Patch.** Whole-project review follow-ups: plugin tools in the catalog,
unambiguous graph naming, a second shipped chain, and project-bound eval
cases. No breaking changes.

- **Catalog covers plugins.** `scripts/gen_catalog.py` now scans
  `plugins/*/tools`, so `graph.*` appears in `docs/catalog.md` tagged with
  its plugin — previously plugin tools were in no reference table.
- **Naming: project graph vs knowledge graph.** graphify's map is now
  called "project graph" everywhere (tool descriptions, the project-prefix
  hint, skill, file-manager UI, docs); `kg.*` keeps "knowledge graph".
  The glossary disambiguates: derived/per-project vs curated/cross-chat.
  Tool names unchanged — no config impact.
- **Second shipped chain:** `knowledge-brief` — recalls from
  memory/kg/RAG first, fills gaps from the web, and marks each bullet
  `[known]` vs `[new]`.
- **Project-bound eval cases.** Eval cases gain `requires_tools` (skip
  cleanly when an install lacks the tools, e.g. plugin disabled) and
  `project` fixtures (files seeded into the per-case sandbox; optional
  graphify graph pre-built via the CLI). New case `graph-orientation`
  guards the "query the graph before grepping" doctrine. Internally this
  adds a server-side-only `run_overrides.config_patch` seam in the loop.

Upgrade: pull, restart, done.

## 1.1.2 — 2026-08-20

**Patch.** The v1.1.1 audit follow-ups (MCP manager robustness at the
YAML↔UI boundary) plus the admin tab reorder. No breaking changes.

- **MCP manager polish.** YAML-defined server names that violate the UI's
  slug rules are flagged at load time instead of blocking every save with a
  surprise 400; the manager shows when the list comes from runtime.yaml
  (first save takes over via config override; deleting *all* servers falls
  back to the YAML definitions — now warned about). Validation type-checks
  url/command/args/timeout_s for non-UI API clients; the Test button honors
  the per-server timeout and always probes fresh; the "mcp package not
  installed" hint no longer vanishes after a save.
- **Admin tabs reordered:** Status, Processes, Presets, Prompt, Config,
  Tools, MCP, RAG, Studio, Plugins, Eval, Flags, Users, Backup — MCP moves
  out of the Tools tab into its own group right after Tools; docs/admin.md
  sections follow the same order.
- **Docs:** admin.md documents the MCP servers section (incl. the args/env
  round-trip limits); configuration.md lists the mcp tool family.

Upgrade: pull, restart, done.

## 1.1.1 — 2026-08-20

**Hardening + MCP server manager.** The 1.1.0 plugin drop gets its audit
follow-ups (two real bugs fixed), and MCP servers move out of raw-YAML-only
editing into a proper admin UI.

- **Plugin fixes (post-1.1.0 audit).** The graphify plugin's build runner was
  imported three times under different module names — three independent job
  registries, so the duplicate-build guard failed across entry points and
  cancel-on-project-delete was dead. All entry points now share one cached
  module (regression-tested). Staleness marking ignored a custom
  `web.projects_dir` — the `on_project_file_changed` hook now receives the
  resolved root (signature gained a 4th parameter; plugin authors see
  docs/plugins.md). Plus: the loader survives malformed `plugins:` config
  instead of crashing boot, `status.json` writes are atomic, security.md
  documents the plugin trust surface.
- **Admin → Tools → MCP servers.** MCP servers were YAML-only and invisible
  in the admin UI (an empty `servers: {}` flattens to nothing in the Config
  editor). Now: list/add/edit/delete (stdio command+args+env or HTTP url,
  confirm-per-call toggle, timeout), a Test button that lists the server's
  tools, and a hint when the optional `mcp` package is missing. Saves apply
  live, no restart.
- **New plugin hook: `project_tools`.** A plugin can declare which tools a
  project-bound run must keep reachable; they are force-added to the frozen
  auto-selected toolset (unknown and admin-disabled names dropped, explicit
  caller tool lists stay authoritative). The graphify plugin uses it to keep
  `graph.*` callable whenever its project hint is injected — the keyword
  selector has no "graph" trigger, so before this the hint could advertise
  tools the model couldn't call.
- **New doc: `docs/playbook.md`** — the tool/skill/chain/plugin landscape in
  prose: what every piece does and is good at, how the pieces harmonize and
  where they compete, ending in a verdict. Written against the
  implementations, not just the descriptions; linked from the README.

Upgrade: pull, restart, done. The `on_project_file_changed` hook signature
changed — only relevant if you wrote a 1.1.0 plugin against it.

## 1.1.0 — 2026-08-20

**Plugin system + per-project knowledge graphs.** JayNet gains an
optional-capability layer: plugins are installable, toggleable bundles that
extend JayNet through a small hook API — disabled or broken plugins are never
imported, so they can't take the core down. The first shipped plugin maps any
project into a queryable knowledge graph.

- **Plugin system.** Two layers (repo `plugins/` builtins, default off;
  `<data>/plugins/` installed, default on), manifest-driven
  (`plugin.yaml` with `requires_jaynet` + pip dependency gates), admin
  Plugins tab with enable/disable (restart to apply). Plugins can contribute
  tools, skills, hooks (`augment_project_context`, `on_project_delete`,
  `on_project_file_changed`) and routes — see [docs/plugins.md](docs/plugins.md).
- **Graphify plugin (builtin, off by default).** Wraps the
  [graphify](https://github.com/Graphify-Labs/graphify) CLI: each project's
  files become a knowledge graph — code via local tree-sitter AST (no LLM),
  docs/PDFs via a semantic pass through your local LiteLLM alias. The agent
  gets private `graph.build/query/explain/path/status` tools and a
  query-before-grep hint in the project prompt; the files panel gets a graph
  bar (build / view / report). The graph lives at
  `<project>/graphify-out/` and is deleted with the project. Enable:
  `uv pip install --python .venv/bin/python graphifyy`, Admin → Plugins → enable, restart.
- `ToolContext.project_id` is now threaded through runs (incl. sub-agents)
  so project-scoped plugin tools resolve their storage correctly.

Upgrade: pull, restart, done. Nothing changes until you enable a plugin.

## 1.0.3 — 2026-08-20

**Hotfix.** One real bug on top of 1.0.2, plus doc-count corrections.

- **Admin → Tools no longer 500s on an undescribed tool.** The new
  per-tool descriptions used `splitlines()[0]` — a custom (Studio) tool
  with an empty description turned that into an IndexError and took the
  whole grid down. Now yields `""`, with a regression test.
- Changelog/release notes: corrected the 1.0.1 audit accounting (9 of 16
  suggestions in code, two more documented as accepted risks) and
  resynced the README version badge.

Upgrade: pull, restart, done.

## 1.0.2 — 2026-08-19

**Self-documenting admin + selftest fix round.** Found by running the
shipped selftest skill against the live install (kimi-k3 as brain) and by a
documentation pass over the admin tabs. Suite 1176 passed, ruff clean.

- **Admin → Config explains itself.** Every setting (~300 keys) shows a
  one-line explanation under its label, served from the new shipped
  `config/config-help.yaml`. A coverage test fails the suite when a key
  ships without help — the on-screen docs can't rot. New
  `docs/configuration.md` maps the config layers (YAML seed → DB overrides
  → per-user/per-run) and walks the editor sections.
- **Admin → Tools shows real descriptions.** The grid's tooltip was always
  empty — the API never sent a description field. Each of the 113 tools
  now carries its one-liner (the text the model reads) inline, and the
  filter matches it.
- **Headless-browser setup is distro-aware.** `browser.*`/`web.render`/
  `pdf.create` failed at RuntimeError on fresh installs (live selftest
  finding): `setup.sh --with-tools` now resolves the platform (existing
  system chromium / pacman / apt / `playwright install`) and
  `orch --doctor` reports which path wins with an install hint.
- **Cloud catalog fixes.** The `gemini-pro` seed pointed at a non-existent
  `gemini-3.5-pro`; the cloud store now rejects an OpenRouter `api_base`
  whose provider model lacks the `openrouter/` prefix — LiteLLM silently
  dropped such deployments and the alias vanished from `/imp`.
- **Eval proposals land cleaner.** Judge meta-phrasing ("Add a
  directive: …") is stripped when a prompt/skill tweak is accepted, so the
  live prompt overlay reads as directives to the model.
- **`code.patch`** — the lenient retry now passes `--recount` (live
  selftest finding).

Upgrade: pull, restart, done — no config or data migration.

## 1.0.1 — 2026-08-19

**Post-release audit round-trip.** The v1.0.0 full bug & security audit,
fixed end to end:
all four A-items, 14 of 17 B-nits, 9 of 16 suggestions in code — two more
are documented as accepted risks in `docs/security.md`, the rest deferred
as product decisions. Suite 1163 passed, ruff clean.

- **Cloud/privacy gates closed everywhere.** `verify.*` accepted a
  model-chosen cloud alias and sent graded content off-box with no approval
  and no taint refusal (the bug class the S1 audit closed for
  council/eval). Slash-command spawns (`/<tool> … model=<cloud>`) skipped
  the cloud spawn gate. Both now gate exactly like `llm.call`.
- **Gate consistency.** `git.fetch` (network egress to the configured
  remote) is confirmation-gated like pull/push; `trace.query` and
  `trace.mine` `all_owners=true` (cross-user trace read) now require
  confirmation.
- **Secrets hygiene.** Serving launches (llama-server) get a
  secret-scrubbed environment instead of the full orchestrator env, and
  `scrub_env` also drops `_PASSPHRASE`/`_PAT`/`_DSN` and `DATABASE_URL`-style
  DSNs. `users.db`/`chats.db`/data dir are chmod 0600/0700 in app code (the
  quickstart path has no systemd `UMask=0077` to rely on); `session.secret`
  is created `O_EXCL` 0600.
- **Supply chain.** quickstart pins llama.cpp (`b10343`) and sha256-verifies
  the download against GitHub's published asset digest (`--latest` opts back
  into floating). Runtime/web/tool deps ship pinned `requirements.lock`
  files, installed by setup.sh/quickstart.sh/CI (loose `.txt` = fallback).
- **Install correctness.** setup.sh, quickstart.sh and the setup doc require
  Python 3.11 (the code needs it; 3.10 used to pass the checks and die at
  import). A cleartext non-loopback bind prints a loud boot warning.
- **Robustness.** Prompt scheduler task can't be GC'd; ProcessManager
  spawn-failures count toward `max_restarts` instead of retrying forever;
  `budget-defaults.json` and `server.json` write atomically; one malformed
  `schedules.json` entry no longer stalls every scheduled prompt; blocking
  HF-metadata and binary-`--help` calls moved off the event loop; the login
  throttle's maps are bounded against unique-username sprays.
- **UI.** Escaping consistency pass in admin.html/app.js (the JSON-array
  config input was a real markup-breakage bug; process cards render via
  textContent/handler closures instead of inline `onclick`).
- **Ops.** Restore mirrors the backup whitelist (stray archive entries like
  `session.secret` are no longer swapped in); `.gitignore` covers `*.env`;
  setup.sh comments out unused provider `<key>` lines; both systemd units
  gain `NoNewPrivileges`/`PrivateTmp`/`ProtectSystem=full`.

## 1.0.0 — 2026-08-19

**Public-release milestone.** JayNet started as a personal learning project
and a nightly-driver experiment; 1.0.0 marks the point where the install is
documented for strangers (quickstart throwaway test, guided setup.sh, manual
path), the API contract is frozen (`docs/api.md`), the suite runs green in
CI on every push, and the codebase is MIT-licensed for everyone to use.

Changes since 0.9.8:

- **Mobile scrolling fixed.** Follow-to-bottom no longer traps touch users
  during a run — a downward finger drag releases it (previously only
  wheel/keys/scrollbar could), reaching the bottom re-engages.
- Housekeeping: the parked-work file is swept to the three real open items
  (GitHub Releases, managed vLLM, Android app); README polish.

## 0.9.8 — 2026-08-16

- **Nerd-mode prompt line, final form.** The ❯ glyph hangs in the log
  gutter (easy to spot, shell-style), the gold shine is back, the 118ch
  measure cap is gone (full-width terminal), and wrapped prompt lines sit
  flush with the first line instead of indenting.
- **Faster boot.** The specialist's boot stagger drops 45s → 20s
  (specialist2/3 keep the 5s ladder at 25/30).

## 0.9.7 — 2026-08-15

- **Structured preset editor.** The raw `.conf` textbox is now a form — one
  field per launch flag `start-model.sh` understands, typed (numbers,
  enums), with defaults and one-line help. `model file`, `mmproj` and
  `tools template` get **browse…** pickers confined to the models dir.
  The raw text stays behind an **advanced (raw .conf)** toggle; switching
  views is lossless (comments and unknown keys survive).
- **Model files browser.** Admin → Presets → **Browse model files…** opens
  the models dir as a collapsible folder tree (`.gguf`/`.jinja` by default,
  **show all** reveals the rest). Files a preset references are marked
  **★ preset-name**, and **Make preset from selected** drafts a new preset
  (name, model path, VRAM estimate) for the picked GGUF.
- **Per-binary flag help.** Each llama-server binary (Admin → Processes)
  has a **help** button showing its `--help` output; the preset form's
  `extra args` row links to the same viewer for the selected binary.
- **Service restart buttons.** Admin → Status can restart `litellm-proxy`
  and the web console itself (delayed + detached self-restart).
- **Fixes:** `start-model.sh` prefers the install venv's python (PyYAML on
  minimal distros/CI) · mobile ⋯ menu font · todo-panel collapse
  specificity · nerd-mode user-line wrap/shine polish · 2026-08-15 audit:
  keyed-endpoint probe shadowing, `$JAYNET_MODELS` conf expansion parity,
  binary-help cache invalidation, self-restart fallback logging.

## 0.9.6 — 2026-08-14

- **API keys for adopted (remote) endpoints.** A remote preset gains an
  **api key env** field (admin → Presets): the NAME of an env var in
  `~/.config/jaynet.env` holding the server's key. The key never enters the
  preset DB or litellm.yaml (rendered as `os.environ/…`); probes send it as
  a Bearer header, and a 401/403 now distinguishes "no key configured" from
  "key rejected". (2026-08-11 audit A3)
- **CI + lint baseline.** `.github/workflows/ci.yml` runs ruff and the full
  pytest suite on every push/PR; `ruff.toml` pins the rule set
  (E4/E7/E9/F/I/UP) after a one-time cleanup pass. Python minimum is now
  3.11 (web/server.py already used 3.11 syntax).
- **Scheduled, version-tagged eval runs.** Admin → Eval → Scheduled runs
  fires a suite unattended on an interval (selector `case:<id>` or
  `tag:<tag>`, 1–720 h) through the normal suite path — skipped while any
  suite runs, auto-disabled when its selector goes stale. Every eval result
  now records the JayNet **version** alongside the brain label, so eval.db
  is a longitudinal quality ledger across releases and brain swaps.
- **Near-duplicate loop guard.** The exact-args loop guard now also catches
  the classic overthinking pattern — the same search reworded ("price 2026
  CHF" → "24h price CHF 2026"). For query-like tools (`loop_guard.
  near_dup_tools`, default web.search/web.fetch/arxiv.search), two calls
  whose argument tokens overlap ≥ `near_dup_threshold` (0.75) count as
  duplicates: the third similar call is blocked with a synthesize-now error
  and feeds the wrap-up escalation. Genuinely different queries pass
  untouched — deep research is unaffected.
- **Benchmark-informed routing.** The Benchmark compare view crowns the
  leading variant (★ winner bar, mean pass rate tie-broken by score) and
  offers a one-click **route it**: assign the winning preset to a slot
  through the existing preset-slots API — human-gated, restart-to-apply,
  closing the shoot-out-then-swap loop.
- **Audit fixes (2026-08-14).** The `live_slot` and /imp dead-slot probes
  now forward a remote preset's API key (a keyed adopted endpoint no longer
  shows dead there); schedule-toggle PUT without `enabled` 400s instead of
  silently disabling; eval version lists sort numerically; CI also tests
  the declared Python 3.11 floor.

## 0.9.5 — 2026-08-13

- **Eval suites and benchmarks can be cancelled** (Admin → Eval): a Cancel
  button / `POST /api/admin/evals/cancel` stops the run after the case in
  flight finishes — later cases are skipped and the summary is marked
  cancelled.
- **Benchmark reps no longer wobble the statistics.** Eval results recorded
  under a benchmark variant are flagged, and the Statistics view (KPIs,
  trend, flakiness, per-case drilldown) counts live runs only by default; a
  new brain dropdown scopes every statistic to one variant label. Results
  recorded before this change stay in the default view.
- **Design refresh across the console.** Tool-call state moved into status
  dots with a gold running band; nerd mode gets a readable 118ch measure
  and a shared glyph gutter; the ctx meter is a fill bar; the admin coral
  was demoted to an accent stripe + ADMIN pill, with status pills carrying
  state dots; account/admin share the app's tokens, micro-label headers
  and tabular numerals. The composer keeps its classic transparent-gold
  icon layout (a circular-rail experiment was tried and reverted), and the
  nerd/chat-bubbles switch is a labeled toggle in the desktop header —
  mobile always follows your stored default.
- **FastAPI startup/shutdown hooks migrated to the lifespan API** — no
  behavior change, deprecation warnings gone.
- **`CONTEXT.md`** at the repo root: a code-facing glossary for AI-assisted
  dev sessions (term → module map), complementing `docs/glossary.md`.
- **`LEARNING_GUIDE.md` corrected and extended** — verified against the
  current code (tool count, eval flow, preset/slot model) and the best of
  the earlier cut material restored.

## 0.9.4 — 2026-08-12

- **`JAYNET_LLAMA` indirection removed.** It existed only to locate a GPU
  env script (`$JAYNET_LLAMA/rdna4-env.sh`) and was a silent no-op when unset.
  `tools.serve.env_setup` now ships empty — set an absolute path in Admin →
  Config if you have such a script. Existing configs that reference
  `$JAYNET_LLAMA` keep working (`$VARS` still expand; the job runner now
  expands them too, like the serve launcher always did).
- **CLI self-bootstraps: `scripts/orch` works as documented.** Run with a
  bare system python it re-execs into the checkout's `.venv` (click/rich live
  there), and it now loads `~/.config/jaynet.env` like the systemd units do —
  previously a CLI run outside the default `/srv/orchestrator` path resolved
  every path wrong (`orch --doctor` reported phantom failures on a healthy
  install).
- **setup.sh survives delete-and-reinstall.** It now stops existing
  `litellm-proxy`/`jaynet-web` units up front and clears `start-limit-hit`
  before enabling — previously a reinstall into a deleted tree left
  `Restart=always` crash-looping the units (203/EXEC) until systemd gave up,
  and the first healthy start needed a manual `reset-failed`.
- **Preset seed is now generic teaching examples.** The wolf-specific
  production presets (Fable/Tess/ornith/agents1/dolphin, Genesis brains,
  8B embedder) are replaced by two commented example presets —
  `brain-moe` (Qwen3-30B-A3B, MoE: ~3B active params = fast all-day brain)
  and `specialist` (Qwen2.5-Coder-32B, dense: stronger per token for code
  delegation) — both without model files, with per-knob explanations in
  `presets/*.conf`. Existing installs are untouched (their presets.db
  already holds the old seed). docs/models.md explains the MoE/dense pair.
- **Self-contained llama.cpp install trees now just run.** `start-model.sh`
  prepends `<bin>/../lib` to `LD_LIBRARY_PATH`, so a cmake-install layout
  (shared libs next to `bin/`) works without `ldconfig` or system-wide
  install.
- **setup.sh pins LiteLLM from `requirements-litellm.lock`** (was the loose
  `.txt`): a fresh install no longer resolves a too-new FastAPI that breaks
  the proxy's imports, and re-running setup heals a drifted litellmenv. The
  lock's uvloop is bumped to 0.22.1 (0.21 doesn't import on Python 3.14).

## 0.9.3 — 2026-08-11

- **HF downloader: chat templates + wired preset suggestions.** Repo
  listing includes `.jinja` chat templates (marked "template" in the UI);
  `create preset` now detects a sibling `mmproj*.gguf` / `.jinja` in the
  same repo and prefills `MMPROJ`+`MMPROJ_OFFLOAD` / `TOOLS_TEMPLATE` in the
  suggested .conf, with a note when the referenced sibling isn't downloaded
  yet.
- **Adopt any OpenAI-compatible server as a remote preset (vLLM, Ollama).**
  Remote presets now accept full endpoint URLs (`http://vllm-box:8000`,
  scheme defaults work) and carry a `backend` label (llama/vllm/ollama/openai)
  plus per-preset `caps` overrides (vision/thinking). Probing matches
  `served_id` across all models a multi-model server reports; the jinja
  thinking switch and vision gating follow backend+caps; keyed endpoints
  (401/403) are reported as "authentication required" — adopted endpoints
  must be keyless for now. Admin → Presets gains endpoint/backend/caps
  fields; existing presets DBs migrate on next start. See
  [docs/models.md](docs/models.md#adopt-existing-server).
- **setup.sh robustness**: systemd unit and env-file path rewrites work from
  any clone directory (were hardcoded to /srv/orchestrator, dying with
  203/EXEC); the first-login credentials (admin + generated password) are
  always printed at the end of setup.
- **Docs**: install guide split into setup_installation.md (scripted) and
  manual_installation.md (by hand); new glossary; manual guide's
  helper_scripts section refreshed (backend menu, no removed scripts).
- **Docs audit fixes**: preset-key table completed to the launcher's real
  vocabulary (MMPROJ/MTP/reasoning/embed keys); remote-preset docs brought
  post-Layer-1 (model-placement, admin); stale live-install references
  removed (testing, development); paths/backup commands corrected
  (manual_installation, upgrading); `/api/voice` config gate documented.

## 0.9.2 — 2026-08-11

- **Quick start: one command to run, stranger-proof prompts.**
  `scripts/quickstart.sh` now writes a `start.sh` that runs the model and
  the web app in a single terminal (Ctrl+C stops both; the exit trap takes
  the model down). Ports are asked interactively (defaults `4000`/`8071`,
  `JAYNET_LITELLM_PORT` / `JAYNET_WEB_PORT` win) with re-ask on taken or
  invalid input — a custom model port is also written into
  `config/runtime.yaml` (`orchestrator.litellm_base`), since quickstart
  runs no LiteLLM proxy. A `ldd` check catches missing shared libraries
  (e.g. `libgomp` on stock Ubuntu/WSL) with the exact apt/pacman package
  hint instead of a raw linker error at first start. `start.sh` re-checks
  its ports and fails with a friendly hint (SO_REUSEADDR probes — no
  TIME_WAIT false positives on quick restarts). All script entry points
  use `python3` shebangs now (stock Ubuntu has no `python`).
- **Quick start default model is Qwen3-1.7B** (was Qwen3-4B): ~1.3 GB,
  2–3× faster on CPU, same family/template with tool calling intact —
  the 4B stays the preset-seed brain for full/GPU installs and is one
  explicit `scripts/quickstart.sh Qwen/Qwen3-4B-GGUF` away.
- **Bare `test` as a first message is a smoke test, not an agent run.**
  The classic first thing a new user types is intercepted in `/api/chat`
  (bare `test` only — no attachments, no history, no project) and answered
  with a liveness probe of the model endpoint: "Smoke test passed/failed"
  with the served model id and a pointer to `start.sh` / Admin → Status.
  In a project, `test` still means "run the tests"; longer messages reach
  the loop as before. The probe sends `LITELLM_MASTER_KEY` when set.
- **README: install-from-scratch pass.** Prerequisite commands for Arch +
  Ubuntu/Debian (incl. `uv`, `libgomp1`), WSL2 note for Windows, the quick
  start framed as a throwaway try-out with a cleanup block, `setup.sh` as
  the fixed install, first-login documents the seeded `admin` user with
  the one-time generated password, and the repo moved to
  `github.com/jspawn/jaynet_orchestrator`.
- **Handoffs for AI-assisted modification** (`handoffs/`): self-contained
  briefings to paste into a fresh AI session — re-theme/replace the web UI,
  create skills, create chains, add tools (Python/connector/MCP) — plus a
  shared ground-rules index (tests, custom layer vs repo, conventions).
- **Remote slots: Stop is guarded too.** Admin → Processes refused
  start/restart on remote slots already; `stop` now returns the same 409
  ("served by \<host\>, probe only") instead of a misleading success.
- **Preset seeds are clone-location independent.** The shipped seed entries
  in `config/runtime.yaml` now use `presets/...` paths relative to
  `JAYNET_HOME` (was absolute `/srv/orchestrator/...`), so a fresh install
  anywhere seeds its preset catalog from the files that ship in the repo.

## 0.9.1 — 2026-08-10

- **Remote presets: local models served by another LAN box.** A preset with
  a `remote_host` (Admin → Presets → *remote* checkbox) is a llama-server
  running elsewhere in the homelab, treated like a local preset — boot
  slots, `model.use`, `model.list`, `local-*` aliases — except JayNet never
  launches/swaps/stops it: the process manager skips remote slots at boot
  (*remote — probe only* on the Processes tab), `serve.start` and
  `start-model.sh` refuse them, and `model.use` only health-probes. Stays
  out of cloud models, so the privacy gate keeps classifying it as local;
  no cost, no key. Plain HTTP on the LAN — see
  [docs/model-placement.md](docs/model-placement.md).

- **Boot slots can be empty; up to three specialists.** Every slot except
  brain can be set to **(none)** (Admin → Presets → Boot model slots) to
  run without that process — skipped at startup, shown as *disabled (slot
  empty)*, manual start refused. An empty specialist keeps its LiteLLM
  alias alive by following the brain. New optional `specialist2` /
  `specialist3` slots (ship empty; new dormant `processes:` entries in
  runtime.yaml) render as `local-specialist2` / `local-specialist3`
  aliases while assigned.

- **ToDos panel: floating tab/card on all viewports.** Collapses to a small
  status tab (JayNet-logo pip: pulsing while working, goldenrod pending,
  red failed, green all done) pinned inside the chat area on desktop and
  mobile; expands to the full step list in place. ToDos clear on the next
  prompt after a run finishes.

- **Rebrand: orchestrator → jaynet in deployment-facing names.** The env
  file moves to `~/.config/jaynet.env` (template
  `example_configs/jaynet.env.example`), the web unit to
  `jaynet-web.service`, and every env var to the `JAYNET_*` prefix.
  **Not breaking for Python**: `runtime/env.py` dual-reads —
  `JAYNET_*` wins, `ORCH_*` still works everywhere in app code, scripts and
  `start-model.sh`. **Breaking for systemd**: the units substitute
  `${JAYNET_*}` from the env file directly, so switching units requires the
  renamed env file — migration steps in `docs/upgrading.md`
  ("Renamed in 0.9.x"). Kept as-is on purpose: the `local-orchestrator`
  LiteLLM alias (fallback chains), the `scripts/orch` CLI, and the internal
  `ORCH_EXEC_OUT` snippet contract.

- **HuggingFace downloader in Admin → Presets**: repo → .gguf file picker
  with sizes, background downloads with live progress + cancel, then
  "create preset" opens the editor prefilled (name, alias, next free port,
  .conf skeleton with `MODEL_PATH`, VRAM estimate). New shared core
  `runtime/hf_pull.py`; `scripts/pull-model` keeps its CLI contract on top
  of it. API: `/api/admin/hf/{files,download,jobs,cancel,preset-suggestion}`.
  `HF_TOKEN` in the service env authenticates both paths (gated repos,
  rate limits); stale `.part` residue is swept from the models dir on
  startup. The env template also drops inline comments — systemd keeps
  them as part of the value.

- **Styled dialogs everywhere** (GUI audit C4): new `web/static/dialog.js` —
  promise-based `dlgAlert`/`dlgConfirm`/`dlgPrompt`, themed via CSS
  variables, Esc/Enter/click-outside — replaces every native
  `alert()`/`confirm()`/`prompt()` across chat, file manager, and admin
  (~40 call sites). Browser "prevent additional dialogs" can no longer
  silently break flows like rename.

Coding-flow upgrades (harness over model — the coding-quality pass):

- **Orientation pack** (`runtime/context_pack.py`): a char-budgeted repo map
  (one line per source file — symbols + imports, cached on a tree
  fingerprint) plus the workspace's `JAYNET.md`/`AGENTS.md`/`CLAUDE.md`,
  prepended to `code.delegate` and architect plan/executor spawns
  (`tools.code.repomap` in runtime.yaml).
- **Verify baseline pre-run**: a verified run's check now runs once BEFORE
  the agent starts; a final failure identical to that pre-existing baseline
  passes as "not worse" (stated in the report), so pre-existing red is never
  chased or blamed on the change. Tamper and vacuous-pass guards unchanged.
- **Isolated delegation**: `code.delegate isolated:true` runs the coder in a
  throwaway git worktree (`.jaynet-worktrees/<id>-<suffix>`, own
  `jaynet/<id>-<suffix>` branch, per-call unique, hidden from the user's
  `git status` via `.git/info/exclude`) via a new spawn `work_root_path`
  kwarg confined to the parent's roots; the tool result carries commit
  count + diff stat + untracked files, only truly empty worktrees (no
  commits, no diff, nothing untracked, inspection clean) auto-clean, and
  merge/discard goes through the confirmation-gated git tools.
- **Per-unit architect verify**: UNITS now parse `- <step> | check: <cmd>`;
  when every unit has a check (`architect.per_unit_verify`, default on),
  each unit runs as its own executor spawn mechanically gated on its check,
  stopping at the first failure — prompt-level self-checking becomes
  harness-enforced.
- **Coding eval suite**: six new cases (`code-bugfix`, `code-refactor`,
  `code-feature-spec`, `code-spec-conflict-trap`, `code-weakened-test`,
  `code-orientation`) covering hidden-test discipline, behavior-preserving
  refactors, TDD order, the spec-vs-test trap, test-weakening honesty, and
  symbol navigation.

Harness todo list (ToDos side panel):

- New `todos` tool + per-run `TodoList` state (`runtime/todos.py`): the agent
  plans multi-step work as a structured list (set/update/add/remove/clear;
  pending/working/done/failed/skipped, at most one working) and the web UI
  renders it live in a collapsible right-edge panel — vertical "ToDos" toggle
  strip (label + done/total count stay visible when collapsed), per-item
  expander with description and the model's notes, collapsed by default. Every change emits a full-snapshot `todos` SSE event
  (reconnect- and replay-safe); the loop re-injects a compact rendering each
  turn so the list survives compaction (its own trailing system message when
  the working anchor is off, folded into the anchor when on). The architect
  flow's UNITS become the list automatically, and a spawned executor's
  updates forward to the parent's panel and state.

Behavioural eval harness (Admin → Eval):

- YAML test cases (`evals/` seeds + `$ORCH_DATA/custom/evals/`) run scripted
  or adaptive multi-turn conversations through the real agent loop — an
  unattended toolset (confirmation-gated tools excluded, except the
  sandbox-confined `fs.write`/`fs.edit` which run auto-approved against the
  per-case sandbox; cloud `llm.call` stays in but auto-denied, so privacy
  gates are really tested), which also redirects the memory/RAG stores, so a
  run can neither pollute real memory nor pull it into a judge transcript —
  graded by a state-aware judge model: it sees the run's available tools,
  the live system prompt, relevant tool descriptions, the bodies of the
  skills the agent loaded, and a config slice next to the transcript
  (`eval:` config section; cloud alias with local-specialist fallback,
  temperature 0). The only budget is $.
- Results, judge notes and pass-rate trends persist in `eval.db`, with a
  Statistics view (KPI cards, daily pass-rate/score trend, per-case
  flakiness, A/B period comparison, per-brain results); failures produce
  deduplicated WHAT/CAUSE/FIX proposals — nothing auto-applies. Accepting
  one applies to the custom layer only: prompt/skill tweaks extend the
  shipped artifact's overlay copy (a skill tweak is live on the next
  `skill.load` — no restart), tool descriptions are replaced via
  `custom/tool-overrides.yaml`, whitelisted config knobs go through the
  override path, bug-for-dev writes a ready-to-paste issue.
- Flags grow an "include private context" opt-in (default off) and a
  "make test" button that drafts a case from a flag's coroner report via a
  local model only — flagged content never leaves the box.
- `eval.run` / `eval.list` / `eval.report` tools let the agent self-test;
  cases share via `.jaypack`. 14 seed cases ship in `evals/`.
- Benchmark shootouts (Admin → Eval → Benchmark): run the same suite under N
  variants — a variant is a label + model alias + sampler overrides (e.g.
  `temperature: 0`, fixed `seed`) + reps — recorded under the label as the
  result's brain, with a per-variant comparison matrix (pass rate / avg
  score / cost / elapsed per case + overall). Pinned sampling applies to
  cross-model variants too (`sampling_force` run-override opt-in); variant
  aliases are validated at submit; a benchmark-wide cost ceiling
  (`eval.benchmark_max_cost_usd`, default $10) caps total spend.

Gate prompt overlay:

- The shipped `prompts/orchestrator-gate.md` stays pristine. Live edits —
  the Admin → Prompt tab and accepted eval prompt-tweaks — write an overlay
  in the data dir that wins while present, apply to the next run, and can be
  reverted to the shipped prompt, so deploys never conflict with live prompt
  edits.

Install simplification + pre-1.0 cleanup:

- `scripts/setup.sh` (full installer: prereqs, venvs, env file with
  auto-generated secrets, systemd units, linger) and `scripts/quickstart.sh`
  (one-command minimal install: prebuilt llama-server + model download)
- `scripts/orch --doctor` — install validator (10 checks with fix hints);
  `scripts/pull-model` — interactive HuggingFace GGUF downloader
  (`ORCH_MODELS`, default `$ORCH_HOME/models`)
- LiteLLM master key now optional for localhost-only installs (render omits
  it when `LITELLM_MASTER_KEY` is unset)
- runtime.yaml typo guard: boot warns on unknown config sections with
  "did you mean …" hints
- Preset hygiene: dead `.conf` keys removed (`PREDICT`, `MAIN_GPU`,
  `SYSTEM_PROMPT` — parsed nowhere), the four portable confs carry
  `HOST`/`PORT` so the documented `--preset` file-mode contract holds,
  `BACKEND` documented as display metadata, chat templates live in
  `$ORCH_MODELS/chat_templates/` (out of the repo), and the launcher's
  `.conf` parser expands `$ORCH_MODELS` textually; the eval cases table's
  Latest column fits 3-digit scores
- Default model set defined (docs/models.md): fresh installs seed
  brain = Qwen3-4B, embed/rerank = Qwen3 0.6B (all Apache-2.0) — code
  fallbacks, shipped presets and quickstart all point there; existing
  presets.db catalogs are untouched (seed applies to empty DBs only)
- Ports (`ORCH_LITELLM_PORT`, `ORCH_WEB_PORT`) and trusted proxy IP
  (`ORCH_FORWARDED_ALLOW_IPS`) configurable via the env file
- Retired `llama-brain1`/`llama-specialist` units (process manager owns
  models); templates moved to `example_configs/` with `.example` naming;
  version shown in the web UI; `docs/models.md` license-clean model picks

Pre-public security hardening (full third-party audit, read-only → fixes):

- **Missing sandbox now fails gated, not open**: when the firejail binary
  isn't on PATH, `code.run`/`code.execute` require human confirmation and
  the verifier refuses to run bare — previously they ran unsandboxed
  *ungated* on any host without firejail (every fresh non-Arch install)
- Browser tools (`web.render`, `browser.screenshot`, `browser.pdf`) now
  intercept every in-browser request and block loopback/link-local/metadata
  targets — closes the redirect-based SSRF bypass of the fetch guard;
  `pdf.create` renders fully offline (all network aborted except data: URIs)
- Web console: paste-jacking XSS in the composer's smart paste fixed
  (inert DOMParser); 2FA confirm/disable now throttled like login; request
  bodies capped (streaming 413s; restore ≤ `web.max_restore_mb`, studio
  import ≤ 5 MB, 4 MB global JSON cap); logout invalidates the session
  server-side; unknown-user login runs a dummy PBKDF2 (no timing oracle);
  new password hashes use 600k iterations (per-hash count, old ones keep
  verifying); admin-created accounts enforce the same ≥8-char minimum
- Agent runtime: a sub-agent spawn is refused when the parent's cost/token
  ceiling is already spent (previously the child ran *unlimited*);
  malformed model tool-calls degrade to an error result instead of an
  internal-error run abort; `trace.log_content: false` now strips every
  content-bearing event kind; gate-prompt overlay + tool-override writes
  are atomic; `job.start` env is scrubbed like `code.run`
- Tools: `git.pull`/`git.push` reject URL/`ext::` remotes like fetch;
  `web.request` drops Authorization/Cookie on cross-origin redirect hops;
  `.jaypack` import rejects decompression bombs (20 MB uncompressed cap)
- Shipped config neutralized: no live LAN IPs (SearXNG endpoint, trusted
  proxy default), no author paths (`$ORCH_MODELS` in presets, relative
  tools templates, binaries seed emptied — existing preset DBs keep their
  values, `$ORCH_LLAMA` expands in `env_setup`); `.gitignore` covers
  quickstart artifacts (bin/, *.bak, .env, *.part)
- Documented (accepted, docs/security.md): scheduled runs auto-approve
  gated tools by default; outbound GETs are an ungated exfiltration
  channel for a prompt-injected agent; managed child processes inherit
  the service env

## 0.9.0

First tagged release. Feature-complete daily driver; the 0.9.x line is
contract-hardening toward 1.0 — see
docs/development.md → Versioning.

Highlights since development started (squashed):

- Web console: multi-user auth (+TOTP 2FA), per-user chats/projects, quick
  settings, run budgets, inline diffs, light/dark theme
- Agent runtime: local-first routing brain + specialist slots, preset
  catalog with GPU/CPU placement, strengths-aware delegation, ~100 tools,
  skills/chains, Studio (admin-created skills/chains/connectors + .jaypack
  share), wiki, memory + KG, trace mining, verify/council/ops tools
- Voice channel `/api/voice` with `voice:false` chat mode for native
  clients; per-user API tokens; SSE streaming; scheduled runs; flags/coroner
- Admin console: status + hardware, processes, presets, prompt, config,
  tools, users, flags, RAG
- Repo hygiene: MIT license, secrets sweep (clean), paths centralized in
  `runtime/paths.py`, nginx example, stable API contract + upgrade guide
