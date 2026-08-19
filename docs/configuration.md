# Configuration

JayNet has three config layers, in rising priority:

1. **`config/runtime.yaml`** — the shipped default, heavily commented. It is
   git-managed: deploys update it, and its diff is the review trail.
2. **DB overrides** — everything you change in **admin → Config** is stored in
   the users DB, applies immediately, and survives restarts. The YAML stays
   pristine; blanking a field (or the ↺ button) resets to the YAML default.
3. **Per-user / per-run** — account settings (budget defaults, architect
   threshold, sampling) and the quick-settings bar tighten a single run.
   Upper layers can only tighten, never loosen.

Every key in the admin editor has a **one-line explanation under its label**
(served from `config/config-help.yaml`). This page is the orientation map —
the inline help and the YAML comments carry the detail.

Env-file settings (ports, paths, API keys, `JAYNET_*` vars) live in
`~/.config/jaynet.env` — see [manual_installation.md](manual_installation.md).

## The sections

- **Orchestrator** — the brain alias, LiteLLM endpoint, turn timeout,
  context-size guard, chat-history cap, timezone/location injected into the
  prompt, and the default `sampling.*` sent per request (UI can override;
  `null` = the model server's own preset default).
- **Budgets** — per-run ceilings: iterations, tokens, cost, wall clock
  (`0` = off; `stall_s` catches hung streams instead), plus
  `warn_fraction`, the point where the model is told to checkpoint.
- **Agent & Verify** — `agent.spawn` nesting depth, sub-agent budgets, the
  working anchor (`anchor.mode`) and todo re-injection, and the verify gate
  (checks, protected test files) for spawned coders.
- **Architect** — the plan-first flow for complex requests: complexity
  threshold, reviewer/arbiter models, per-unit verification.
- **Tool Selection** — which tools the model sees: `auto` = core set +
  keyword-triggered namespaces, with `tools.load` as the mid-run escape
  hatch. The `keyword_namespaces.*` entries are the trigger word lists.
- **Compaction** — shrinking old tool results in the transcript to stubs:
  size threshold, how many recent results to keep, pass frequency.
- **Parallel Tools** — run independent approved tool calls of one turn
  concurrently.
- **Privacy & Confirmation** — the taint model (private results never reach
  a cloud model without approval) and the human-approval gate for
  state-changing/cloud calls. Details: [security.md](security.md).
- **Voice** — the `/api/voice` endpoint for native clients: persona overlay,
  model, tighter per-turn budget.
- **Trace** — the run/event database: content logging toggle, retention.
- **Web / UI** — DB paths, upload/output limits, scratch-workspace TTL, and
  `cookie_secure` (only with HTTPS — breaks login on plain HTTP).
- **Costs** — USD-per-1M-token table used for budget accounting. Local
  models are 0; every cloud alias in `litellm.yaml` needs a row or it
  silently bills $0. Seed only — afterwards edit in admin → Presets → Cloud
  models.
- **Verify / Council** — the LLM-as-a-verifier knobs (grade scale, GBNF
  constraint, repeats) and the default debate panel.
- **Models & Presets** — seed for the preset catalog DB (slots, GPUs,
  placement). After first boot the DB wins — edit in admin → Presets.
  See [models.md](models.md).
- **Tools: …** — per-tool-family knobs: `ops` (allowlisted host commands),
  `code`/`lint` (sandbox, interpreters, delegate model),
  `web`/`browser` (search backends, headless rendering),
  `serve` (managed model servers: ports, GPUs, health checks),
  `rag`/`research` (embedding/rerank endpoints, dedup),
  `test` (the pytest harness), `schedule` (the tick and its budget).
  `call_timeout_s` is the hard per-call backstop;
  `call_timeout_overrides.*` relaxes it for legitimately slow tools
  (`0` = unwrapped, for self-bounding orchestrators like `agent.spawn`).

## For contributors

Adding a key to `runtime.yaml` without a matching entry in
`config/config-help.yaml` (exact dotpath or a `pattern`) **fails the test
suite** (`tests/test_config_help.py`). One short line: what it does, plus
the one caveat that bites (units, what `0`/`null` means).
