# Changelog

Breaking changes and release notes. Versions are git tags; the stable API
contract lives in `docs/api.md`, upgrade procedure in `docs/upgrading.md`.
Every tagged version gets a release file in `docs/releases/vX.Y.Z.md`
(cut from this changelog — don't let it drift again).

## 1.20.3 — 2026-10-06

Doc/quality audit S-batch (items #2, #4, #12, #14, #15) — docs
infrastructure and freshness gates, no runtime behavior change except
one config cleanup.

- **"Life of a turn" page + generated rail registry.** New
  `docs/turn.md` walks the exact execution order of one loop
  iteration (pre-turn guards → model turn → tool-call gates /
  final-answer guards → verifier) as a mermaid flowchart; new
  `docs/rails.md` is the generated registry of every guard and
  dispatch gate (name, order, purpose, config keys, live case),
  produced by `scripts/gen_rails.py` from the actual registries.
  CI fails when it drifts (`gen_rails.py --check`; renamed gates
  break the build via source anchors).
- **Complexity ceiling for new code.** `scripts/check_complexity.py`
  enforces ruff C901 at 25 with a committed baseline
  (`tests/complexity-baseline.txt`, 18 grandfathered hotspots) —
  NEW violations fail, and a grandfathered function getting MORE
  complex fails too (same mechanism as the mypy baseline). CI ruff
  now also covers `plugins/`.
- **Docs index by audience.** New `docs/README.md` routes readers
  (chat user / operator / contributor / AI session) to the right
  docs; every doc carries a one-line audience header. The README's
  "Example setup (wolf)" section moved to `docs/my-setup.md`,
  marked example-only.
- **Docs tool-reference freshness.** `scripts/check_doc_tools.py`
  scans all docs for dotted tool names and fails on ones that don't
  exist in the registry (config-key/record-field lookalikes filtered,
  small hand-reviewed skip list). Caught two real oddities on first
  run. Config-help deduplication (#14a) needed no new mechanism —
  already covered both directions by existing pytest guards.
- **Learning guide split.** `LEARNING_GUIDE.md` is now Part 1 "how it
  works now" / Part 2 "how we got here" with a TOC; original section
  numbers preserved so cross-references stay valid.
- **Dead security config entry removed.** `studio.python` in
  `security.admin_only_tools` matched no tool (Studio custom tools
  register as `custom.*`; the entry dated from the original
  role-policy commit). Removed from the shipped config and
  `docs/security.md`, which now states the actual posture: custom
  tools are admin-authored via `/api/admin/studio` and run as
  trusted as built-ins.

## 1.20.2 — 2026-10-06

Harness fixes from the v1.20.0 delta-fail dig (63/82; two trace
investigations over all 19 fails).

- **Vacuous specialist-authored checks now fail review
  deterministically.** Live: a specialist's `assert … or True`
  (tb-huarong-dao) and a literal `python3 -c "print('ok')"`
  (gaia-65afbc8a) both came back `verified: true`.
  `review_delegation` lints the authored check before any model call:
  tautologies/can't-fail shapes and checks referencing none of the
  task-named deliverables fail closed (no LLM spend); `files_changed`
  missing a task-named output is flagged in the review evidence.
- **Stall ladder: fresh diagnostics no longer escalate; real writes
  disarm the hard stop.** Live (tb-regex-log): six DISTINCT diagnostic
  `code.check` turns (new info each) counted as no-progress, the stop
  armed, and the `fs.write` carrying the diagnosed fix was BLOCKED —
  the run ended stating a fix it wasn't allowed to apply. Now:
  non-repeat check turns (fresh args AND fresh result) are neutral on
  the ladder, and `fs.write`/`fs.edit` pass the armed stop (a real
  mutation disarms it; byte-identical rewrites stay blocked).
- **`code.run`/`code.check` environment honesty.** The description
  claimed cwd = project root for python snippets (they actually chdir
  to ORCH_EXEC_WORK — brains burned calls on `./solution.json`
  FileNotFoundError), and bash-shaped input (`cd …`, `python3 <<EOF`)
  with `language=python` now gets a loud re-issue hint instead of a
  NameError.
- **Bot-wall pages get the js=true hint.** An Anubis proof-of-work
  challenge (1068 chars) beat the 500-char thin-content threshold and
  returned as "ok" content (gaia-72e110e7). Bot-wall markers (Anubis,
  cf-challenge, "just a moment…", …) now mark a page thin regardless
  of length, attaching the headless-browser retry hint.
- **Prompt vs brain code gate aligned.** The gate prompt said
  "compute with code.run" while `brain_mode: verify` removes code.run;
  it now names `code.check` as the brain's compute/verify lane (same
  engine, network off, 120s/200-line caps), and code.check's
  description no longer points at a tool the brain can't call.
- Ops note: the searXNG container had been down ~7 days during the
  delta ("web search degraded" in judge notes) — restarted, end-to-end
  verified.

## 1.20.1 — 2026-10-05

Audit follow-up (claude_audit_05102026): all 14 September findings
verified resolved; the three new findings fixed here.

- **`test.run` / `code.deps` are admin-only now (High).** A non-admin
  account could get a service-user shell through `test.run` (`bash
  -lc`, sandbox prefix defaults to `[]`) — confirmation is not a
  cross-account boundary. Both tools join `security.admin_only_tools`
  (enforced at dispatch, slash, and /goal); refusal tests pin the
  shipped list and the dispatch path.
- **`fs.edit` fails safe (Medium).** The forgiving matcher could
  silently mis-edit and still report success: whitespace normalization
  collapsed newlines+indentation (a statement could migrate out of its
  block), and line-prefix stripping ate legitimate `new_str` content
  (dict keys like `1: 'one'`). The matcher is now line-anchored —
  leading indentation must match exactly, only intra-line space/tab
  runs collapse — and `new_str` is de-prefixed only when EVERY
  non-empty line carries the prefix shape. The result already carries
  the diff; pinned by tests.
- **Knowledge stores are owner-scoped (Medium).** `memory.*`, `kg.*`,
  `rag.*`, and pageindex were single stores shared across all web
  accounts. They now filter by `ctx.owner` with an admin-only,
  confirmation-gated `all_owners` escape (the trace tools' exact
  pattern). One-time migrations stamp existing rows with the first
  admin's owner (kg tables rebuilt for per-owner composite uniques);
  pageindex storage moves to `<data>/pageindex/<owner>` (legacy root
  stays the ownerless CLI store — pre-scoping indexes remain reachable
  there, no cross-user leak).
- Low items: small-brain context/thinking tuning parked in
  ToDos_for_later.md (brain-dependent; cybertiel-35B doesn't need it);
  guard-ablation + prompt diet already tracked.

## 1.20.0 — 2026-10-05

- **First live `model.measure` round: two launch-path fixes the fakes
  never told.** (1) `serve.start` *requests* a port, but the
  dispatcher's `--preset` file mode lets the `.conf` own the slot — and
  the materialized confs carried no `PORT`, so the measurement server
  quietly bound the `:8080` default while the ready-wait polled the
  requested catalog port for 600 s. Materialized confs now carry the
  catalog's `PORT` / `VISIBLE_DEVICES` appended (name-mode PM boots
  capture and ignore them — unchanged). (2) Some llama.cpp
  builds answer `/v1/models` in the legacy `{"models": […]}` shape; the
  readiness probe only read OpenAI `{"data": […]}` and never fired —
  `query_model_ids` parses both now. The hibernate → restore path
  validated live instead: the failed measurement still brought brain
  and specialist back on its own.
- **`scripts/slash-run.py` — live-ops driver for slash commands.**
  Fires a slash command at `/api/chat`, streams `/api/stream/{id}`,
  auto-approves confirmation prompts (a 30-minute `model.measure`
  would die on the 300 s confirm timeout otherwise), prints the final
  answer. Same pattern as `eval-peek.py` / `eval-delta.sh`: drive the
  live API, never the live checkout.
- **Docs catch-up for v1.18.4–v1.19.0:** measured scheduling across
  `llama-ops` / `model-placement` / `models` / `code-map`; imagegen
  `swap_slots` + h5i full verb surface in `plugins.md`; j-space
  ceremony salvage + tamper transparency in `playbook.md`;
  `slash-run.py` in `development.md`; LEARNING_GUIDE §3.21 (incl. the
  two live-find lessons above).

## 1.19.0 — 2026-10-05

- **Preset-measured memory scheduling (`model.measure` + fit-aware
  loader).** `vram_gib` was a hand estimate and the eviction planner
  treated ANY co-tenant on a pinned GPU as a conflict — a specialist at
  50% of GPU1 blocked a second model that would have fit. New
  `model.measure` tool (confirmation-gated, disruptive): hibernates the
  whole box (brain included, restored afterwards), loads the preset,
  fires one probe completion, and writes the REAL per-GPU VRAM + RAM
  usage into the preset (`measured` JSON — new presets.db column with
  migration, round-trips through export/import). The loader now
  schedules by fit: with a current measurement, a new model loads
  ALONGSIDE existing tenants when every pinned card has free ≥ share +
  floor — eviction only on genuine shortfall (port conflicts still
  always evict); CPU presets get the same RAM-fit rule
  (`models.min_free_ram_gib`, default 2.0). Measurements go stale on
  preset edits (ctx/gpu change → falls back to the estimate).
  `call_timeout_overrides` gains `model.measure: 1800`. Docs:
  `docs/llama-ops.md` "Measured scheduling".

## 1.18.6 — 2026-10-05

- **imagegen: hibernate several slots — brain included — for a big
  generation.** `swap_slot` was a single slot, so a big image could free
  the specialist's VRAM but never the brain's (the other GPU tenant).
  New `swap_slots: [specialist, brain]` config (merged with `swap_slot`,
  back-compat) stops every listed slot and restores them cancel-safe in
  reverse order; a hibernated brain is restored IMMEDIATELY after the
  generation with a readiness wait (`restore_ready_timeout_s`, default
  300 — port discovered via the preset store, falling back to the launch
  command) so the parent run's next turn never hits a dead model. The
  tool result names what was hibernated and any slot that missed its
  readiness window.
- **h5i: the full red-team verb surface.** `browser.recon` gains
  `paths` (wordlist probing, request-bounded like crawl);
  `browser.websec` gains `experiment` (one request many ways,
  clustered), `matrix` (one request under several identities —
  authz/IDOR), `sequence` (multi-step flows with bindings), `socket`
  (WebSocket frames), `dom` (prototype-pollution/DOM-XSS probes),
  `grpc` (describe/call) and `import-nuclei` (template → h5i-test
  file); new `browser.test` replays portable attack flows with
  repository-owned oracles (h5i-test — `requires_confirmation`,
  AUTHORIZED TARGETS ONLY). Plans/wordlists inline (scratch file) or
  from the workspace; the abstract-base catalog warning is gone.

## 1.18.5 — 2026-10-05

- **Auto-delegate can no longer dodge the j-space gate (live validation
  find).** j-space-loop rep 1 failed the "plan before edit" rubric: the
  brain badged, stalled on reads without planning, and the loop guard's
  own salvage lane (`_auto_delegate`) handed implementation to the
  specialist directly — bypassing the dispatch path where the badge gate
  lives. The harness-side delegation now closes the ceremony first,
  honestly attributed: it badges `j-space: full` when the brain never
  did, records a minimal salvage plan (delegate / verify / answer)
  through the run's own todos wiring, latches the gate, and says in the
  run that the LOOP GUARD, not the brain, performed the ceremony. The
  salvage lane keeps working — the work still gets done — but no edit
  lands ahead of a plan, and the record shows who planned.
- **Undeclared test edits now die with the remedy attached (live
  validation find).** First live run of the `allow_test_edits` era: the
  brain delegated "adjust the test…" in prose but never declared the
  arg; the specialist did the work correctly, the tamper guard killed it
  exactly as designed, and the opaque "verifier stuck" error left the
  brain flailing until the stall guard closed tools (j-space-loop rep,
  child "unverified", rename discarded). The tamper death now names the
  changed protected files in the delegate result and presents both
  readings — possible test-weakening (the guard's job) vs. legitimate
  edit → "re-issue with `allow_test_edits: ['…']`" — reachable even
  under stall hard-stop, which still offers `specialist.delegate`. The
  arg description now leads with the consequence: undeclared = killed as
  TAMPERING, work discarded.

## 1.18.4 — 2026-10-05

- **Native media players render dark in dark mode.** The dark theme never
  declared `color-scheme`, so the browser fell back to light chrome for
  native controls — the new `<audio>`/`<video>` players rendered white.
  `:root` now declares `color-scheme: dark` (`body.light` keeps its
  `light` override); other native controls (scrollbars, form inputs)
  follow the dark palette too. css buster bumped (`?v=48`).
- **Cache-buster test: clean-file false positive fixed.** The v1.18.3
  gate joined two empty `git diff` outputs to a truthy `"\n"`, reading
  any clean asset as an uncommitted edit — it went red the moment the
  tree was clean after the release commit. Clean files now compare
  against committed history as intended.

## 1.18.3 — 2026-10-05

- **j-space badge gate: closed the bypasses the soft nudge already knew
  about (audit #28 C1).** The hard gate tested only `fs.write`/`fs.edit`,
  so `code.run` heredocs (`cat > f <<EOF`), `sed -i`, `code.patch` and
  `agent.fanout` all executed unbadged and unplanned — proven in the real
  loop. The gate now uses the repo's existing `_gate_write_like` predicate
  (fs.* + `code.patch` + shell writes via `code.run`/`code.execute`/
  `code.check`) and gates `agent.fanout` beside `agent.spawn`; the
  `.jspace/` ledger exemption is preserved. Pinned with tests for all four
  bypass shapes plus read-only shell commands staying free.
- **Gate state no longer lives inside a removable nudge (audit #28 C2).**
  `rs.badged`/`rs.badge_watch` were written solely by `BadgeWatchGuard`, so
  the documented ablation `guards_off: ["badge_watch"]` silently disabled
  the default-on gate while config still showed it on — and
  `guards_off: ["jspace_badge_gate"]` was rejected as unknown. Both fields
  are now set from the `run.badge`/`skill.load` tool results in the loop's
  post-call path, the guard owns only the reminder text, and the gate is
  registered as a guard name so it can be ablated by its own name.
  Regression-pinned: nudge ablated → gate still fires. The rejection text
  also names `"j-space: fast"` as the honest badge for one-step work
  (D1) — the skill's fast pass no longer forces full/loop ceremony.
- **`agent.verify.unprotect` is a real config key, and waivers are
  normalized, diagnosed and visible (D3/D4).** The key was read from
  config but undeclared, so the loader answered "unknown key — did you
  mean 'protect'?" (the opposite knob). Declared in the schema +
  config-help. Declared exemption paths are now normalized on both sides
  (`./x.py` ≡ `x.py`, absolute paths inside the work root relativized); a
  declaration matching nothing produces an explicit "protection still
  applies" diagnostic instead of dying as the exact "verifier stuck"
  failure the feature exists to fix; and every applied waiver leaves a
  `[tamper waiver active — exempted: …]` record in the verify report +
  state, so trace.db can tell a waived run from a clean one.
- **Timeout wrapper: overrides for the tools that outlive 180 s
  (#27 C1).** Live trace: `agent.fanout` killed 12× and `llm.call` 17× at
  exactly 180 s (all vision/OCR calls). Added `llm.call: 660`,
  `lint.run: 360`, `image.generate: 1260`, and `0` (orchestrator-exempt)
  for `agent.fanout` and `chain.run`. A new completeness test fails when
  a listed tool's internal budget exceeds `call_timeout_s` without an
  override above it.
- **pageindex: documents no longer leave the box ungated (#27 C3).**
  Building an index sent document text to any configured alias, cloud
  included, while plugin.yaml/docs claimed "nothing leaves the box". The
  build path now runs the core cloud-gate machinery (local alias → fine;
  cloud + private taint without `share_private` → refused; cloud builds
  ask via `needs_confirmation`). doc.tree/doc.pages are local store reads
  and stay ungated; the "nothing leaves the box" claims are qualified.
- **Media plugins: cancellation-safe cleanup and honest artifact
  hand-off (#27 C2/D6/D7/D8).** A `CancelledError` mid-generation skipped
  imagegen's slot restore and left both plugins' keep-warm reaper
  disarmed — both now clean up on `BaseException` and re-arm before
  re-raising. Artifact filenames carry a uuid suffix (same-second calls
  no longer collide), the workspace mirror re-copies when content differs
  instead of silently skipping, the returned `path` can never point at an
  already-deleted file, and `audio.clone` refuses reference WAVs over
  25 MB before reading them.
- **omnivoice vocabulary: the last two stale copies fixed (D5, #27 D4).**
  `plugins/omnivoice/plugin.yaml` named non-existent tools
  (`voice.speak`/`voice.clone` → `audio.speak`/`audio.clone`) and both it
  and `docs/playbook.md` still taught the free-prose emotion form the
  server rejects 100% of the time — both now teach the fixed vocabulary.
  The read-aloud chain's `voice` step gets a bounded budget (12
  iterations, was 16) and the no-swap-back warning moved from a comment
  into the user-visible `description:` (#27 D5 chain half).
- **`eval-ab.sh`: trap, honest restore, loud failures (D2).** The script
  restored `state_file` to ON — the position its own A/B rejected (+30%
  tokens, ~2× wall) — had no `trap` (any abort left live config mutated
  and the service restarted), dirtied the git-tracked live runtime.yaml,
  and its `sed` could silently no-op so both arms ran with the same flag.
  Now: `trap … EXIT` restores flag + restarts on any exit,
  `RESTORE_FLAG` defaults to `false`, every flag flip is asserted, a dirty
  live tree warns, and the header states the single-flag scope.
- **`agent.state_file`: bounded, off-thread state.md read (D8).** The
  per-turn re-read loaded the whole file synchronously on the event loop
  and capped only after reading; `fs.write` append can grow the file
  unboundedly. Now `stat()` first, tail-only read over the cap, through
  `asyncio.to_thread` — the event-loop-blocking class closed for the
  fourth time.
- **Cache-buster bumped + a gate so it can't lapse again (D7, 4th
  recurrence).** `app.js`/`app.css` changed for the inline players with no
  `?v=` bump — cached clients never saw v1.18.2's headline feature.
  Bumped, and a new git-history test fails when app.js/app.css change
  without the buster line changing.
- **Docs/config hygiene (#27 D1/D2/D3, D6, obs 10).** Doubled breadcrumb
  "Studio & Eval → Studio & Eval → Proposals" fixed in all three copies;
  25 retired admin-tab references swept across 20 files (incl. 11 presets,
  scripts, model-facing strings) against the real Models/Harness layout;
  the `default_sub_iterations` comment now says 16, matching the shipped
  default; `docs/configuration.md` + glossary document
  `loop_guard.jspace_badge_gate` (incl. the `BLOCKED (j-space badge gate)`
  string users hit) and `agent.state_file`; the clm-bakeoff "route stays
  false" verdict is scoped to the cloud backend so it no longer
  contradicts the live local-jevify route.

## 1.18.2 — 2026-10-05

- **Inline audio/video players in chat.** Generated audio (omnivoice WAVs,
  mp3/ogg/flac/m4a/opus/aac) and video (mp4/webm/mov/ogv/m4v) deliverables
  now render as inline `<audio>`/`<video>` players where the deliver
  happened — same treatment images got; `preload=none` keeps saved chats
  from fetching every clip up front, the download chip and "open in tab"
  stay. Backend serves real media types on `?inline=1` (no CSP sandbox —
  media isn't markup).
- **omnivoice: teach the real voice-design vocabulary.** Live failures
  ("instruct 'dark, deep, ominous…' could not be resolved") showed the
  server's `instructions` is NOT free prose — it's comma-separated items
  from a fixed vocabulary (male|female, age band, pitch band, whisper,
  accents), one per category. The `audio.speak` arg description, the
  omnivoice skill, the read-aloud chain's VOICE-line guidance and the
  docs all taught free-text emotions ("afraid, whispered") that the
  server rejects 100% of the time. All now teach the vocabulary
  ("male, very low pitch" is the "dark voice"), and HTTP error bodies
  pass through at 2500 chars (was 300) so the server's did-you-mean +
  valid-item list actually reaches the model for a one-shot retry.

## 1.18.1 — 2026-10-05

- **`specialist.delegate allow_test_edits` — caller-declared exceptions to
  the verify gate's tamper protection.** Live delegations died as "verifier
  stuck on the same failure 2× (not converging)" when the task REQUIRED a
  test edit ("adjust the test so it checks the new name"): the specialist
  edited `test_service.py` exactly as instructed and the tamper guard
  (correctly, for every undeclared case — `code-weakened-test` depends on
  it) killed the run. The calling brain can now declare relative
  test/check paths the task legitimately modifies; the declaration lands
  as `verify.unprotect` on both the explicit-verify and the `auto_verify`
  path and is dropped from both sides of the tamper comparison. The arg
  is never exposed in the child's toolset, a mis-shaped value is ignored
  (a bad shape must never widen the exception), every unlisted protected
  file keeps full protection, and the exit-code/vacuous-pass checks are
  untouched. `agent.spawn` passes the key through unchanged.

## 1.18.0 — 2026-10-04

- **Per-turn budget visibility (`agent.anchor.budget`, default on) — the
  brain finally sees its iteration budget.** Every model turn now ends with
  a one-line readout (`budget: iteration 3/8`, used/limit; just the used
  count when uncapped) at the prompt tail: inside the working anchor when
  `agent.anchor.mode` is on, standalone at the todos re-injection slot
  otherwise — the `agent.state_file`/todos pattern, rebuilt per turn, never
  persisted into the transcript, so it survives compaction by construction.
  Fixes the recurring eval-flake class where the brain over-verified
  trivial answers and over-searched because it could not pace itself
  against a budget it never saw. `false` = zero injection.
- **Evals enforce their stated iteration budget.** A case's declared
  `expect.max_iterations` now flows into the run's real budget (was: a
  post-hoc judge check only — a runaway case burned the whole wall-clock
  allowance before failing). 0/absent still means the loop default. The
  post-hoc per-turn check stays as belt-and-braces and tolerates the one
  extra tick an enforced stop records (`Budget.tick()` counts the tick that
  trips the ceiling, so a run capped at N ends at N+1 with status
  `budget_exceeded`; the final-synthesis turn is never ticked and cannot
  push it further) — a run capped at the limit that synthesizes a final
  answer no longer fails "used N+1 iterations". Validated live: the flake
  set that motivated it (datetime-awareness, fs-roundtrip, ask-user,
  j-space-loop, budget-clean-exit) went 5/5 on the first post-fix delta.
- **j-space protocol gate (`loop_guard.jspace_badge_gate`, default on).** The
  3x repeat told the truth the 5/5 hid: budget visibility fixed pacing but
  NOT protocol compliance — j-space-loop failed 3/3 on the sole
  deterministic check (`run.badge` never called; all the work done right).
  So it's a hard gate like the dispatch gate: once the j-space skill is
  loaded, project edits stay rejected at dispatch (never executed) until
  the run is badged — and the first live validation immediately showed the
  next dodge: the brain badged, then routed implementation through
  `specialist.delegate` in dispatch mode without ever planning. The gate
  now covers both lanes and both protocol steps: `fs.write`/`fs.edit`
  outside `.jspace/`, `specialist.delegate` and `agent.spawn` all stay
  blocked until `run.badge` was called AND a todos plan exists; the
  rejection names only the missing step (classify fast/full/loop → badge →
  plan → work). The `.jspace/` ledger flow stays writable, the gate
  latches open permanently once both are in place (a later `todos clear`
  can't re-arm it), and rejections count toward
  `loop_guard.max_rejections`, so a brain that refuses gets the standard
  wrap-up endgame instead of spinning to the cap.
- **`scripts/eval-ab.sh` — reusable two-arm flag A/B driver.** Fires a case
  list N times per arm against the live admin API, edits the live
  runtime.yaml + restarts jaynet-web between arms, restores the flag,
  writes per-suite JSON + a summary table. Crash-safe continuation via
  `ARMS`/`REP_START`/`OUT_DIR`. Built for the state_file A/B, kept for
  future flag decisions.
- **Self-managed state file (`agent.state_file`, default off) — CLM-style
  agent continuity memory.** An adaptation of the CLM paper (Context
  Language Models, arxiv 2609.37725): the agent maintains `state.md` in its
  workspace with the `fs.*` tools it already has (no new tool), and the loop
  re-injects the file as a trailing injection every model turn — after the
  transcript, immediately before generation. Suffix placement is the point:
  mid-prompt edits re-prefill everything after the edit point on llama.cpp,
  so the volatile content goes last and the prefix cache survives. The
  injection is rebuilt per turn and never persisted into the transcript, so
  it survives compaction by construction; the harness-summary compaction
  path is untouched and stays the fallback. Rides inside/alongside the
  working anchor exactly like the todo list when the anchor is on.
  `agent.state_file.max_chars` caps the injection (over-cap keeps the newest
  tail and notes the truncation in the header); `agent.state_file.instructions`
  is the evolvable instruction overlay (injected once at run start),
  config-overridable so the eval-proposals loop can tune it without a code
  change. Absent or empty file means zero injection, zero cost. A/B'd live
  (8 cases x 3 reps per arm): no pass-rate benefit, +30% tokens, ~2x wall
  time — stays default OFF, code kept (docs/clm-bakeoff.md).

## 1.17.0 — 2026-10-01

- **pageindex plugin 0.1.0 — vectorless tree index for long PDFs.** Wraps
  the [pageindex](https://pypi.org/project/pageindex/) pip SDK (MIT):
  `doc.index` builds a persistent hierarchical tree index of a long document
  with a local model (through the JayNet LiteLLM proxy — nothing leaves the
  box), `doc.tree` shows its section titles/summaries/page ranges, and
  `doc.pages` reads exact pages — the brain navigates structure and page
  ranges instead of vector-similarity chunks. For reports, contracts and
  manuals too long to read inline, and repeat questions over the same
  document (index once, reuse the doc_id); `doc.extract` and the
  long-document skill stay right for short docs. New `plugins.pageindex`
  config section (default disabled, needs `pip install pageindex`), a `doc`
  keyword namespace for auto tool selection, and the `pageindex` skill.
  Setup in plugins/pageindex/README.md. SDK return shapes verified against
  a live index run (page entries use `page_index`; the page count is a
  `get_document` meta lookup, not part of `submit_document`'s return).
- **omnivoice plugin 0.1.0 — local text-to-speech.** `audio.speak` turns
  text into a WAV on your own GPU (600+ languages, voice design via
  `instructions`, non-verbal symbols like `[laughter]`) and
  `audio.clone` registers a cloned voice from a reference WAV + exact
  transcript — [OmniVoice](https://huggingface.co/k2-fsa/OmniVoice) via
  [omnivoice.cpp](https://github.com/ServeurpersoCom/omnivoice.cpp)'s
  OpenAI-compatible `tts-server`, no cloud. Mirrors the imagegen shape:
  the server starts on first call, a keep-warm reaper (default 600 s)
  shuts it down when idle, the WAV is staged as a user download AND
  mirrored into the run workspace (the imagegen path-gate lesson), and a
  shutdown hook downs the server with JayNet. No slot hibernation — the
  Q8_0 pair is ~1 GB. New `plugins.omnivoice` config section (default
  disabled), an `audio` keyword namespace for auto tool selection
  (speak/read aloud/voice message/tts/…), and the typed schema default
  to match. Build + models: `./build_tools.sh omnivoice rocm` (helper
  repo) + ~1 GB GGUFs, setup in plugins/omnivoice/README.md.
- **omnivoice: GGUF auto-discovery.** `plugins.omnivoice.model`/`codec`
  left empty now pick up the first `omnivoice-base-*.gguf` /
  `omnivoice-tokenizer-*.gguf` in the standard model dir instead of
  failing with "not installed" — dropping in a different quant needs no
  config edit. Explicit paths still win.
- **imagegen/omnivoice: server-side artifacts no longer pile up.** The
  backend's own copy in `<data>/images/` / `<data>/audio/` was redundant
  the moment the tool staged the download bundle (delivery serves from
  the bundle, the workspace gets its own mirror) but stayed on disk
  forever. Both plugins now drop the original once staging succeeds and
  keep it only when staging failed (it's the only artifact then).
- **new chain: `read-aloud`.** Expressive text-to-speech as a pipeline:
  step 1 loads the creative-writing preset into the specialist slot via
  `model.use` (default `hemmingway`; degrades gracefully to the current
  specialist when the preset is missing), step 2 splits the text into
  mood-tagged chunks (per-chunk voice-design instructions — the only way
  emotion can shift mid-text), step 3 voices each chunk with
  `audio.speak` and stitches the WAVs into one `reading.wav`. Chains
  couldn't switch models before — this is the first that does.
- **docs: doubled tab-path sweep fix.** The v1.16.1 tab-rename pass left
  "Admin → Studio & Eval → Studio & Eval → …" in 9 markdown files;
  collapsed to the single tab name.

## 1.16.1 — 2026-10-01

- **audit #26 fixes** (report-only audit of the v1.15.1→v1.16.0 window:
  h5i red-team browser, 6-tab admin console, graceful endings):
  - **C1 — `on_run_end` no longer blocks the event loop.** `hooks.fire()` is
    sync and the shipped h5i handler runs a blocking `h5i browser close`
    subprocess (up to 10s) — on the loop thread of a one-process service
    that froze every other run's token stream. The loop now fires the hook
    through `asyncio.to_thread`, covering every present and future plugin.
    The payload also carries a `config` snapshot, and the h5i session reaper
    resolves the binary from `plugins.h5i.binary` first — on a non-PATH
    install the reaper silently never ran (the pile-up it exists to
    prevent).
  - **D2 residue — retired tab names swept.** The 16→6 admin reorg left
    ~107 references pointing at tabs that no longer exist, concentrated in
    user-visible places: config comments and `config-help.yaml` strings
    (rendered in the Runtime editor), model-facing tool notes
    (model/catalog, delegate, mcp client, transcribe, cloud models), skills
    loaded into prompts, handoffs, and the Presets help text inside
    admin.html itself. All now use the new paths (Models → Servers/Presets,
    Harness → Runtime/Prompts/Tools/Integrations/Plugins/Data,
    Studio & Eval, Status & Usage).
  - **D4 — the update check's outbound contract tells the truth.** Config
    and help text claimed "outbound calls only when an admin clicks Check
    now", but the card auto-loads with the console. The texts now say what
    happens: probes fire when an admin opens the Status page (cached 24h),
    Check now forces a fresh one.
  - **D5 — `updates.enabled` help moved out of the council block** into its
    own section, and the delegate-cap help's stale "fleet-wide 8" corrected
    to 16 (the default changed this cycle, one screen apart).
  - **D6 — h5i screenshot path clamp.** `browser.browse action=screenshot`
    built its `--out` path from the raw model-supplied session name —
    `../../../../x` as session would have landed the PNG outside the
    scratch dir. The name is clamped to a safe filename now, and the
    `tempfile.mkdtemp()` fallback (leaked a directory per call) is
    `gettempdir()`.
  - **Folded-in observations:** `_status` version compare degrades to
    `unknown` when the two tag shapes differ (the silent inversion behind
    D7's "current while 40 builds behind"); model-supplied ids/urls reach
    the h5i CLI only after a `--` separator (flag injection into h5i's own
    parser, verified against the installed binary); the chat lightbox's Esc
    listener is removed on every close path, not just Esc; `TurnModel.time`
    gets the same capping validator as `atts`; `check_updates()`'s dead
    `config` parameter dropped.
  - **Two static gates from the audit's bottom line:** every literal
    `$("#id")`/`getElementById` in admin.html must resolve to a markup id
    and every TABMAP pane/sub target must exist (the reorg's load-bearing
    contract, pinned for future reorgs); and every `screenshots/*.png` a
    doc references must exist (the sweep's renames can no longer 404 a doc
    image silently).

## 1.16.0 — 2026-10-01

- **image.generate mirrors the PNG into the run workspace.** The canonical
  artifact lives in `DATA/images` — outside the workspace, where the path
  gate refuses every file tool. Live, a brain burned several calls `cp`-ing
  the file over by hand after `deliver.files` refused it (and a
  vision-recheck via `llm.call` failed on the same gate). The tool now
  copies the PNG into the run's `work_root`, returns that path, and says
  plainly in its result note that the download is already offered and
  `deliver.files` must not be called. Skill updated to match.

- **Admin console reorganization: 16 flat tabs → 6 top-level tabs with
  subtabs.** The tab bar overflowed on tablet-width screens and pages ran
  endless. New structure: **Status & Usage** (Overview / Usage / Recent
  runs), **Models** — the model-switching config in one place (Servers:
  processes+binaries+GPUs / Presets / Files / Cloud), **Harness** —
  everything around the model (Runtime / Prompts / Tools / Integrations /
  Plugins / Data & Backup), **Studio & Eval** (Studio / Eval / Results /
  Proposals / Benchmark — the old 12-block Eval page split by concern),
  **Flagged Chats**, **Users**. Usage per user moved to Status & Usage →
  Usage (it's analytics, not account management). Pure reorganization:
  every section byte-identical, all element ids preserved, lazy loaders
  fire per subtab, hash routing supports `#<top>/<sub>` plus redirects for
  all 16 old hashes, tab bars scroll horizontally with an edge fade when
  they overflow, two-column grids stack below 900px. Stale in-UI
  navigation references ("the Processes tab"…) updated to the new paths.

- **Update check: llama b-tag walk + binary PATH fallbacks.** The first
  live run exposed two bugs: GitHub's `/releases/latest` pointed at a
  non-b tag ("v0.5.0"), so llama.cpp showed "current" while 40 builds
  behind — the check now walks the recent releases and takes the newest
  `b####` tag (the same rule quickstart uses). And `jevify` showed
  "missing" while installed — uv tools live in `~/.local/bin`, which the
  systemd PATH lacks; version probes now fall back through
  `/usr/local/bin` and `~/.local/bin`.

- **Screenshot sweep + docs synced to the admin reorg.**
  `scripts/screenshot_pages.py` now walks the 6 top tabs × subtabs
  (`ADMIN_SHOTS` table, subtab click path, per-shot redaction) and produces
  one PNG per subtab; docs/admin.md is restructured to the new layout (all
  technical content preserved), and every navigation reference in
  README/docs/plugins (incl. the benchlab model-facing strings that feed
  catalog.md) points at the new paths. 12 stale screenshot files deleted;
  the remaining set regenerates on live after the next deploy.

- **Chat roll fixes.**
  - **Attachments persist in turns.** An image posted with + rendered only
    live — after a reload, on another device, or in a saved chat the
    message showed text only. Turns now carry `atts: [{id, name, kind}]`
    (client snapshot + `TurnModel` + a new `chat_turn.atts` JSON column
    with migration; the validator strips unknown keys and caps the list;
    downloads stay owner-scoped).
  - **Tool-call chains are tighter and indented** — `.calls` gap 5px→2px,
    row min-height 24→20px, and the whole block moves `margin-left: 30px`
    (16px below 760px) so process reads visually separate from the answer.
  - **Brain comments readable** — `.seg.comment` was flat `--muted`; now a
    55% foreground mix (adapts to light/nerd themes).
  - **Consecutive same-tool rows group into one expandable "×N" row.**
    Five `web.search` in a row now collapse to `✓ web.search ×3` (with an
    error tally when members failed); one click expands the members.
    Grouping happens at finalize time only (running rows are never folded),
    works live and on saved-chat replay.
  - **Attachment lightbox** — clicking a message or composer thumbnail
    opens it full-screen; click/Esc closes.
  - **Turn separators carry the time** — "— turn 3 · 14:02 —" (per-turn
    `time` through the client snapshot, `TurnModel`, and a `chat_turn.time`
    column; older turns simply show no time).

- **h5i plugin 0.2.0: the red-team browser.** h5i 0.4.x turned the browser
  into an agent security-testing workbench — the engine is the HTTP client,
  so the page driven and the traffic produced are one auditable session.
  `browser.browse` gains `submit`, `scroll`, `waitfor` (selector/text),
  `structured` (JSON-LD/OpenGraph/meta — property portals publish listings
  there; try it before scraping HTML), `transcript` (media captions),
  `screenshot` (PNG, `return_image` shows it to a vision brain with the
  same pixel budget as the Chromium lane), `audit` (full evidence
  timeline), and `capture: true` on `open` (records every request). The
  description, plugin README and docs/plugins.md now carry the red-team
  workflow (recon/websec plugins as separate binaries) with the scope
  discipline: authorized targets only, no complete PoC no vulnerability.
  The stale "no screenshot pipeline" note is gone (h5i screenshots now;
  PDFs stay Chromium). With the recon/websec plugin binaries installed,
  two more tools join: **browser.recon** (endpoint ledger: extract /
  endpoints / known / crawl / triage / show / export — candidate vs
  confirmed is h5i's honesty discipline) and **browser.websec** (the HTTP
  workbench: requests / show / replay with mutations / diff / match /
  sitemap / finding with message-id evidence). Both are `private` — raw
  captures carry credentials, so results stay in the box by default.
  Deliberately not wrapped (v1): recon paths/import/merge/jobs, websec
  experiment/matrix/sequence/socket/import-nuclei.
- **`on_run_end` plugin hook + h5i session reaping.** browser.browse opens
  a per-run h5i session (`jaynet-<run-id8>`) and the model almost never
  calls `close` — h5i keeps a live browser process and registry record per
  session, so they piled up (~80 "still live" sessions blocking
  `h5i browser rm`). New plugin hook `on_run_end(payload)` fires once per
  run on every terminal path (ok/error/cancelled/budget) with
  `{"request_id", "status", "tools"}`; the h5i plugin's hooks.py closes the
  run's default session from it (deterministic name, no shared state;
  explicit `session=` overrides stay user-managed). Cleanup of the existing
  pile: `h5i browser rm --ended` for finished ones, `h5i browser rm
  jaynet-* --force` for the live leftovers.

- **Update check (Admin → Status → Updates card).** Report-only
  installed-vs-upstream version comparison for the external components:
  h5i (GitHub releases), jevify (PyPI), the litellmenv proxy venv (status
  measured against the `requirements-litellm.lock` pin — behind-the-pin is
  the real drift, newer PyPI is informational), and the registered llama.cpp
  binaries (per-binary build numbers; newest upstream b-tag). Each row
  carries the upgrade command; nothing ever auto-updates. Results cache 24h
  in `DATA/update_check.json`; **Check now** forces a fresh probe. New
  config section `updates.enabled` (default on) closes the endpoint when
  false. Motivation: live drifted invisible — litellmenv ran 1.87.0 while
  the lock pinned 1.102.1, h5i was six patch releases behind.

- **Graceful endings for stalled/capped runs** (live trigger: a house-search
  research child found the right portal URLs, got two fetches blocked by the
  stall hard-stop, then died at its 8-iteration cap returning "(no answer
  produced yet)"):
  - **Stall hard-stop escalates to wrap-up when unsalvageable.** Once the
    refusal streak reaches `loop_guard.auto_delegate_after` and the harness's
    own auto-delegation can't salvage the run (no delegate route, or it
    failed), tools switch off for the forced synthesis turn instead of
    refusing until the iteration cap. The wrap-up announcement text is now
    gate-neutral (it's no longer only the duplicate-call path).
  - **`agent.final_synthesis` (default on):** a run killed by
    `max_iterations` after gathering material gets ONE final no-tools model
    turn — findings so far plus a "what's unverified" list, returned with a
    `[Partial — iteration budget exhausted]` marker. Model error, empty
    content, or tool calls on that turn fall back to the old termination
    text; runs without any tool output skip it. `false` restores the raw cap
    (eval harnesses).
  - **`agent.default_sub_iterations` 8 → 16.** The old default was sized for
    "rename this function" and cut multi-source research children off
    mid-gathering. A ceiling, not a target — fast children are unaffected.
    The `agent.spawn`/`specialist.delegate` `budget` arg descriptions now
    tell the brain to pass an explicit higher `max_iterations` for
    research-shaped tasks.
  - **Synthesized partials survive the cut-off envelope.** A child answer
    carrying the synthesis marker is structured findings+unverified, not a
    raw mid-thought — `cutoff_child_answer` now passes it to the parent with
    a roomier cap (4000 vs 1500 chars) and continue-oriented advice
    ("follow-up delegation with a higher budget") instead of the GVS5H
    change-strategy hint. Raw truncated partials keep the old treatment.
  - **Near-dup guard respects lane-switching flags.** Boolean args added no
    tokens to the near-duplicate comparison, so `web.fetch js=true` — the
    retry the 403 error hint itself advises — was blocked as a near-duplicate
    of the plain GET (live: house-search child, turn 5). Calls now only
    compare within the same boolean-flag signature (`_flag_sig`); the js
    lane stays guarded against its own reworded repeats.
  - **Gate-neutral guard labels:** the wrap-up progress event and the
    watchdog churn reason said "blocked duplicates" — stale since the stall
    hard-stop and other gates also drive `guard_rejections`. Both now say
    "refused tool calls".
  - **Chat renders bare URLs as links.** The markdown renderer only
    linkified `[text](url)`; bare URLs (research answers are full of them)
    stayed plain text. Link-producing replacements now stash their HTML so
    the bare-URL pass can't nest inside an existing href/data-src/link
    text; `&amp;` stays correct in hrefs, entity boundaries and trailing
    sentence punctuation are not part of the URL. Backticks and `*` are
    excluded from the URL match too: models wrap URLs in `code`/emphasis
    spans, and eating the closing marker into the URL let the span regex
    pair the leftover opener with the marker inside the href — broken
    `<code>`/`<a>` nesting that compounded the 0.92em code shrink after
    every link (live: house-search answer on mobile).

- **Dependency CVE fixes** — urllib3 2.8.0 (CVE-2026-97687/8/9) in
  requirements.lock + requirements-tools.lock, pyjwt 2.15.0
  (CVE-2026-101918) in requirements-tools.lock + requirements-litellm.lock;
  all three locks audit clean.

## 1.15.1 — 2026-09-30

- **audit #25 fixes.**
  - **B1:** the jev sidecar lifecycle test ran start+stop in two separate
    `asyncio.run` calls — the supervisor's cancel-and-drain never returned
    on py3.11's thread-based child watcher, hanging the floor suite (and
    CI's 3.11 arm) forever. Both hooks now run inside one loop; the py3.11
    floor suite is green again (2046 passed).
  - **D2 (false positive):** the claimed hot-disable lifecycle gap was
    already covered — the admin toggle awaits a plugin's recorded
    shutdown hooks BEFORE unregistering them (web/routes_plugins.py), so
    the jevify sidecar and imagegen's sd-server do stop with the toggle.
    What was actually wrong: the stale jev routes comment that claimed
    the opposite (and misled the audit) — corrected.
  - **D1:** api.md's `/api/health` example shows shapes, not pinned values
    — the second release cut that shipped it one version behind.
- **CI: the 3.11 floor arm runs weekly + on dispatch, not per-push.**
  Per-push is 3.14-only (the interpreter dev + live run) — the floor
  promise stays tested without paying dual-matrix on every commit.
- **CI pytest with xdist `-n 2`** (~130s vs 213s serial) after the 15-min
  job timeout cancelled runs; conftest re-anchors each worker to a private
  data dir (the shared users.db admin-seed race). Wall-clock burns removed
  from the suite's slow tests (18s×4, 8s×2, 10.5s dev-box wait).
- **Install catch-up:** setup.sh copies an explicit systemd unit list (the
  clm-serve plugin template no longer installs a unit that can only fail;
  the opt-in backup timer now installs), quickstart pins llama.cpp b11282
  and `--latest` walks recent releases for the newest b-tag (upstream's
  /releases/latest can point at an asset-less tag).
- **Docs:** README names Cyber-Tiel as the production brain in the two
  spots that still said Ternary-Bonsai; the helper-script section matches
  the real build_tools.sh (whisper/sd/piper projects, versioned install
  prefixes, `update`); development.md documents the `-n 2` suite run.

## 1.15.0 — 2026-09-30

- **New champion brain: Cyber-Tiel-35B-A3B (38/41, first TB clean
  sweep).** The routing-trained MoE (~3B active params, ~106 tok/s) won
  the fourteen-candidate bakeoff with 81% voluntary delegation — the
  "models won't delegate" problem solving itself — plus the first
  `ask-user` pass in any recent era. Table and lessons:
  `docs/brain-bakeoff.md`.
- **Post-delegation bookkeeping spin fixed.** Once a delegation returns
  verified, the stall ladder swaps its generic "produce a deliverable
  NOW" rungs for a wrap-up directive ("the work is DONE — write your
  final answer"). The old advice fed todos-rewrite loops until the
  iteration cap. Validated live: `code-bugfix` FAIL→PASS (443s→216s,
  21→7 iterations) and `code-spec-conflict-trap`'s first pass in any
  era.
- **jevify sidecar is a managed process.** `plugins.jev` can register
  the jevify routing sidecar with the process manager
  (`manage_sidecar` + `recipe`): boot start, auto-restart, clean
  shutdown, `~/.local/bin` PATH fallback for systemd units. Route bench:
  jevify over the specialist hits 63.4% routing accuracy (keywords:
  13.2%).
- **Specialist A/B: turbo keeps the slot.** qwen3.8-flash-coder went
  8/11 vs turbo's 10/11 on the delegated coding cases — no wrong-code
  failures, but ~2x tokens per case. Documented in the bakeoff file.
- **Chat renders deliverables inline** — images/SVG as images, PDF/HTML
  in sandboxed iframes, instead of download-button-only artifacts.
- **imagegen is discoverable** — the plugin ships a skill and an
  `image` keyword namespace, so "draw/generate" requests find
  `image.generate` without a manual `skill.load`.
- **Eval justice** — bookkeeping tools (todos/pin/badge) pass the stall
  hard-stop without disarming it, and three evals accept `code.check`
  as execution evidence.
- **Mermaid skill** — the render-and-verify diagram workflow
  (mermaid-cli), exportable as `.jaypack`.
- **Developer docs** — new `docs/code-map.md` (mechanism → file, with
  entry points) and a mermaid architecture diagram at the top of the
  README.
- **Dependency CVE fixes** — pyjwt 2.14.0, oauthlib 4.0.0 (CI
  pip-audit).

## 1.14.3 — 2026-09-29

- **imagegen goes live + self-delivers.** Local text-to-image
  (Qwen-Image-2.1 GGUF via stable-diffusion.cpp sd-server) proved
  end-to-end on the live box: `image.generate` hibernates the
  specialist slot, serves the diffusion backend, stages the PNG as a
  user download (the first smoke generation landed the file but
  `deliver.files` refused the out-of-workspace path — the tool now
  stages it itself), and the keep-warm reaper restores the slot.
  sd-server is registered for shutdown (no GPU-resident orphan when
  JayNet stops mid keep-warm) and the reaper runs as a tracked
  background task.
- **audit #24 fixes.**
  - **C1:** scheduled runs pass `scratch_key=run_id` — the unattended
    launcher was the last unkeyed caller, so a nightly run starting
    mid-chat wiped the chat's scratch dir (and two concurrent
    scheduled runs wiped each other).
  - **C2:** the delegation-review model call respects the cloud/taint
    gate — a privacy-tainted run restricts the reviewer alias chain
    to local models (tools have no per-call confirm seam, so it fails
    safe like `cloud_gate.privacy_refusal`). Previously a cloud-pinned
    `tools.code.delegate.model` shipped task + report + evidence
    off-box ungated.
  - **D1-D6:** api.md version cite, catalog regen + a CI freshness
    check that fails on gen_catalog drift (third cycle running),
    re-shot presets/plugins screenshots, brain-bakeoff totals row
    filled (six columns), sd-server shutdown registration, mypy gate
    fails closed when the binary is missing. Carry-over docs:
    development.md CI pythons (3.11+3.14), configuration.md stall
    keys (`agent.stall_check.*`), playbook.md shape list, tests.*
    import prefix in three test files.
- **delegate child iterations 24 → 32** — tb-huarong-dao-solver's
  specialist died at 25/24 in two brain eras, one short of the
  solver.
- **Brain bakeoff: Bonsai-27B 32/41, qwen35-9B 26/41.** Two new
  columns + lessons 19-20 in docs/brain-bakeoff.md; todos
  empty-update hint + code.check "not the executor" description from
  the Bonsai judge notes.

- **New plugin: `clm` — Contrastive-LM System One decisions (successor of
  the jev/jevify experiment).** [CLM-8B](https://github.com/Contrastive-LM/CLM)
  is a purpose-built contrastive decision model (state + candidates in,
  calibrated probabilities out — no generation, ~30 ms warm): on par with
  Jev zero-shot at up to 9× lower latency, SOTA verifier on Terminal-Bench
  2.1. The plugin is stdlib-only and wire-compatible with the System One
  contract: `clm.decide` (choice/noul/score), `clm.rank` (best-of-N
  ordering), and an opt-in `route_request` hook for strength routing
  (ships off — keywords stay default until the A/B). The sidecar pair is
  documented in `plugins/clm/README.md`: the new
  `presets/embed-qwen3-8b-clm.conf` encoder preset (Qwen3-8B pooling,
  :8094, ~5.5 GB VRAM) plus `clm-serve` in its own venv
  (`systemd/clm-serve.service` template included). Local-only — no cloud
  backend. jev stays as the reference implementation with a pointer.
- **Auto-delegate: the loop guard now takes the hint itself
  (`loop_guard.auto_delegate_after`, default 2).** The refusal gates
  (stall hard-stop, strength gate, dispatch gate, delegate gate) always
  NAMED `specialist.delegate` in their error text — but the bakeoff
  blind-spot autopsy (23 cases where small brains failed and never
  delegated while the 4B passed) showed frozen brains retry the blocked
  call past 10+ explicit rejections, then collapse into a literal
  `<tool_call>` string at wrap-up. After two delegate-pointing refusals
  the harness runs the delegation itself: harness-picked strength (the
  stuck-delegate route picker), de-anchored raw request (like
  fresh-retry), report injected as a system note. A successful hand-over
  is real progress — it disarms the gates and cancels a pending
  rejection-cap wrap-up. Once per run, brain depth only, silent when no
  specialist routes (single-model installs unchanged). 0 disables.
  Repeat refusals also shrink to a minimal BLOCKED string — at
  refusal-streak depth the long explanation is context poison.
- **Delegation review: judgment moves off the brain
  (`agent.verify_delegate_review`, default on).** A finished ok delegation
  gets a fresh-context review turn on the strongest available model — a
  live verify-tagged slot → the specialist that did the work → the
  allround slot, never the brain. The reviewer sees only task + report +
  evidence and answers `{verdict, issues}`; a `fail` attaches a loud
  `review_warning`. Advisory — the deterministic `verified` flag
  (authored check / verify gate) is unchanged. Pin a preset tagged
  `verify` for a true cross-model second opinion.
- **devbox: first code.exec in a run no longer eats exactly 60s.**
  `_podman` piped stdout/stderr and waited on pipe EOF — but
  `podman run -d`'s detached conmon inherits the pipe write-ends and
  holds them for the container's lifetime, so every fresh devbox
  "timed out" at 60s, fell back to firejail, and orphaned the
  actually-started container (the real reason `12x21` took 78s). Output
  now goes to temp files and we wait for process EXIT — immune to
  grandchildren holding fds.
- **devbox reaper: ghost state files are dropped, not kept forever.** A
  stale state file whose container no longer exists (`--rm` already
  cleaned it up) made every sweep pay a failing `podman stop` for it —
  live: 102 ghosts ≈ 60s of serialized podman calls starving the
  storage lock on a run's first exec (`12x21` took 78s). Files whose
  containers are gone are now dropped; the failed-stop protection for
  LIVE containers is unchanged.
- **deliver.files: actionable "path not found".** The error now says to
  write the file first and that command stdout is never saved to a file
  — the live brain retried a blind deliver twice after `code.check`
  printed `252` to stdout and it assumed `answer.txt` existed.

## 1.14.2 — 2026-09-24

- **routing: route_decision telemetry — the jev experiment is finally
  measurable.** Every depth-0 run now records a `route_decision` trace
  event: where the tag came from (`jev` | `keyword` | `none`), the tag,
  confidence, hook latency, and the keyword baseline (always computed,
  so jev-vs-keyword agreement is queryable from trace.db without
  re-running). The routing hook contract extends to
  `{tag, confidence, source}` dicts (plain strings still accepted).
- **audit #23 fixes.**
  - **B1:** the final-turn tool registry is always built complete;
    `guards_off` skips only the guard *checks*, so the deliverable pin
    survives ablation instead of vanishing with the guards.
  - **C1:** scratch keys are now explicit — `run(scratch_key=…)`; web
    chat and voice key by conversation_id, child runs auto-key by
    run_id, killing the cross-run scratch bleed class for good.
  - **D1-D4:** static asset cache-bust (?v=44), catalog regen,
    changelog/admin.md/api.md/LEARNING_GUIDE drift fixes.

## 1.14.1 — 2026-09-24

- **devbox: the reaper no longer kills fresh containers.** Two stacked
  bugs produced `no such container` ghosts ~30s into fanout runs (39
  hits in one delta): podman's `StartedAt` (`… +0200 CEST`) never
  parsed, so the orphan sweep's young-container guard NEVER fired and
  every stateless box was stopped on sight — and `ensure()` wrote the
  state file only *after* `podman run`, handing the sweep exactly such
  a stateless box. Timestamps now parse (named zone stripped,
  unparseable fails safe = never stop) and the state file is pre-booked
  before the container starts.
- **loop: rewrite loops can't hide from the stall ladder anymore.** A
  byte-identical repeat of an earlier *successful* call (live: 45× the
  same fs.write over 20+ minutes) slipped both rails — repeat-block
  only tracks errors, and each success re-bumped the mutation
  generation, resetting the ladder and the duplicate guard's window.
  Identical twins of earlier OK calls now count as no-progress; distinct
  mutations still reset (edit→test cycles unaffected).
- **Presets can be archived.** Retired presets kept their strength tags
  and tag-based swap routing picks the FIRST tag holder in dict order —
  a shelved duplicate hijacked `coding` swaps live. Admin → Presets now
  has an `archived` checkbox: shelved presets stay listed (greyed),
  never route, and `model.use` refuses them with a clear error.
  Reversible, unlike deletion.
- **ZDTaichu5.0 chat template** (`presets/chat_templates/
  zdtaichu5_tools.jinja`): the stock qwen3.5-family template raises on
  mid-history system messages — every guard nudge would kill the run.
  The shipped variant renders them inline instead (same fix family as
  qwen3.6_tools.jinja).

## 1.14.0 — 2026-09-23

- **fs.read speaks plain text; fs.edit forgives.** File contents no longer
  reach the model as JSON-escaped strings (`\n`/`\t`/quote soup) but as
  plain text with a one-line header — line numbers stay (edit anchors).
  Trace evidence: 4% of all edits failed "old_str not found", most with
  the model's `old_str` carrying a copied `     3\t` line-number prefix.
  fs.edit now falls back exact → prefix-stripped → whitespace-normalized
  (spliced into real file bytes) → closest-snippet error with line
  numbers, and says which fallback fired. Ambiguity errors list the
  matching line numbers.
- **Stall hard-stop (`loop_guard.stall_hard_stop`, default on).** After
  the final stall-ladder rung, tool calls close: only
  `specialist.delegate`, `ask.user` or a final answer pass the dispatch
  gate — refused at call time, never by mutating the tool list (prompt
  cache stays intact). Real progress disarms. Bakeoff lesson 9: the brain
  ignored three stall warnings and spun for 47 minutes; now the last
  warning is the last spin. Emits `stall_hard_stop` + `guard_fired`.
- **Specialist-authored checks (`agent.verify_delegate_authored_check`,
  default on).** When a coding delegation has no project test command,
  the specialist must first write a small check encoding the task's
  examples, run it, and end its report with `CHECK: <cmd>`. The harness
  parses that line (strict last-line contract) and executes it through
  the same sandboxed verify runner as auto_verify — the brain reads a
  deterministic `verified: true/false` with the raw output tail instead
  of a 4B judging a 27B's diff. Fail-closed when the sandbox is missing.
- **Hard-block repeat loops (`loop_guard.hard_block_repeat_errors`,
  default 3).** The 4th identical attempt — same tool, same args, same
  error — is refused at dispatch without executing, with the redirect
  spelled out ("change the approach or the tool"). Live evidence:
  gaia-e142056d burned 2,500s calling `code.check` 26× into the same
  closed-tool error; every nudge guard fired, none stopped it. Covers the
  loops the duplicate guard structurally misses: poll-safe tools, pre-exec
  gate rejections, repeats across mutation generations. 0 disables;
  emits `repeat_blocked`.
- **The loop is a guard pipeline now.** `AgentRuntime.run` (2,300 lines,
  complexity ~500) was restructured per the audit: per-run mutable state
  lives in `runtime/run_state.py` (`RunState`), and every rail is a
  registered guard class — `runtime/turn_guards.py` (pre-turn, post-tool)
  and `runtime/final_guards.py` (final-answer). Event names, payloads and
  firing order are unchanged (the fake-model regression suite proves it;
  step 1 was AST-identical). Every guard application also emits a uniform
  `guard_fired` event, so each rail gets a measurable fire rate. The
  extraction surfaced a real bug: the verify gate's `stall_after` shadowed
  the stall ladder's own config key — `agent.stall_check.after` was parsed
  but dead; now honored (defaults identical, no behavior change on shipped
  config).
- **Bounce cap (`agent.max_bounces_per_answer`, default 3).** A single
  final answer could be bounced up to 8 times — each bounce a full model
  turn over growing context (minutes on a 30 tok/s model). At the cap the
  answer is accepted with a `bounce_cap` event naming the suppressed
  guard; 0 disables.
- **Typed config (`agent`, `budgets`, `tool_selection`, `eval`).**
  pydantic models (`runtime/config_schema.py`) now validate those four
  sections at load: values coerce once in place (`"40"` → 40), unknown
  nested keys log a did-you-mean warning (`agent.stall_check.aftr` no
  longer dies silently), uncoercible values warn and pass through. Saves
  via the admin config editor return the same warnings; nothing is ever
  rejected (backcompat).
- **Guard ablation variants.** Benchmark variants accept
  `guards_off: [names]` — run the fixed case list with one rail disabled
  and keep the rails that pay (the audit's monthly-ablation recipe).
  Unknown guard names fail at validation, before any spend.
- **One subprocess helper, no more leaked children.** New `runtime/proc.py`
  replaces ~15 hand-written `wait_for(communicate())` copies; on timeout it
  kills the whole process group (SIGTERM → SIGKILL) and reaps, so orphaned
  grandchildren (the `sleep 30` that held a test's pipes for 30s) can't
  survive. Migrates goals `| check:`, ops.run, lint, h5i, graphify,
  benchlab and others — verify.py was quietly leaky too.
- **Background tasks are tracked.** `spawn_background()` holds a strong
  reference, logs task exceptions (eval-suite failures no longer vanish)
  and `shutdown_background()` cancels + awaits from the server's shutdown
  path — same-loop only, so cross-loop test artifacts can't break it.
- **CI hardening.** pip-audit runs over all three lockfiles (litellm's
  under Python 3.13 — ≤1.93 can't resolve on 3.14); a mypy baseline gate
  fails only on NEW errors; ruff gains RUF006 + ASYNC with the real
  findings fixed. `requirements-litellm.lock` refreshed: litellm 1.87.0 →
  1.102.1, 69 known advisories → 0. Devbox tests no longer need a podman
  binary on the host; the hf_pull lifecycle test is deterministic.
- **Role-based tool policy (`security.admin_only_tools`).** Non-admin
  accounts no longer reach admin-grade tools: `ops.run`, `job.*`,
  `serve.*`, `model.use`, `git.push`, `mcp.call`, `studio.python` and
  `schedule.add` are admin-only by default — hidden from tool selection
  AND refused at dispatch (also via the `/ops.run` slash path, which
  bypasses the loop). `auto_confirm` is forced off server-side for
  non-admin sessions (chat, goals, voice, schedules), and `/goal … |
  check: <cmd>` is refused for non-admins — the check ran unsandboxed
  as the service user. Closes the audit's P1: any logged-in account
  could shell out as the service user and read `jaynet.env`.
- **Stable per-chat scratch dir.** The loop's scratch dir was
  `orchrun-<run_id>-…` inside the system prompt — a different path every
  run, defeating llama.cpp's prefix cache: every user message re-prefilled
  the whole replayed history. Scratch is now `<work_root>/.tmp/scratch`
  (emptied at run start, defensive never-escape-work_root check; per-run
  temp remains the no-work_root fallback). System messages are
  byte-identical across runs in a chat again.
- **LiteLLM response cache: local aliases opt out.** The proxy cached
  responses for 10 min for every alias; a repeat `council.vote` got N
  copies of one cached answer (fake unanimity), judge re-grades and
  llm.call one-shots could replay. Rendered local aliases now carry
  `cache: {no-cache, no-store}` (llama.cpp's prompt cache covers the
  useful part), `council.vote` samples carry per-ballot nonces, and the
  eval judge appends a per-grade nonce so cloud-cached re-grades can't
  replay a verdict.
- **Eval provenance + fallback visibility.** Result rows gained
  `git_sha`, `git_dirty`, `prompt_hash`, `config_hash`,
  `specialist_preset`, `model_files` and `fallback` columns — any
  before/after question is now a query. The model client captures the
  served `model` of every response; a served-vs-requested mismatch
  (LiteLLM `fallbacks:` silently routing specialist work to the brain)
  is logged, recorded in `model_turn` events as `served_model`, and
  tags the eval row `[fallback: requested X served Y]`.
- **Uncertainty-aware bakeoff stats.** `scripts/eval-peek.py` prints the
  Wilson 95% interval next to each pass rate, and `--compare A B` pairs
  the latest per-case results of two brain labels with McNemar's exact
  test — bakeoff columns like 18/32 vs 12/35 overlap within their
  intervals; unpaired single-rep differences inside the noise band are
  not results.
- **Eval stability is per-brain.** Skip-stable ("Run delta") now counts a
  case as stable only when its last 3 passes were recorded under the
  *current* brain preset — swapping the brain invalidates inherited
  streaks, so the first delta under a new brain plays the full library
  again. Eval rows now record the brain-slot preset name as their brain
  label (was the static LiteLLM alias, which never changed across swaps);
  benchmark-variant labels are unchanged. Unlabeled legacy rows count
  toward any brain.
- **Verify-the-delegate bounce (`agent.verify_delegate_check`, default
  on).** A run that delegated implementation (`specialist.delegate` with
  coding/multi-step) but ran no check tool (`code.check`/`code.run`/
  `code.execute`) *after* the last delegation gets its final answer
  bounced once: "verify the specialist's report, then answer." Live
  evidence from the NeoHorse delta: tb-regex-log delegated twice and
  shipped a regex matching 1/9 dates; code-bugfix checked *before* the
  fix and claimed "no bug exists." One-shot per run; research delegations
  don't arm it (different verification shape), and stating why no check
  applies clears it.
- **Just-reply bounce (`agent.just_reply_check`, default on).** A request
  with compute/fresh-data markers ("how many", "latest", "decode", "the
  most", …) whose run ends having called NO tool at all gets its final
  answer bounced once: "do the computation/lookup first, or say why no
  tool applies." The overnight Spark delta's fast-fail cluster was exactly
  this — just-replied "12000" where computation gives 16000, multi-hop
  questions answered from memory, fetched pages ignored. The trigger only
  arms on marker phrases and only fires on tool-less runs, so plain chat
  is untouched and overreach costs one clarifying turn. Phrases
  overridable via `agent.just_reply_keywords`.
- **Prompt pass (16-habits audit, batches 1+2).** The exact-spec/FINAL
  ANSWER directive moved to the recency slot (last in the block);
  `web.search`/`web.fetch`/`rag.search` descriptions now carry explicit
  fallback contracts (nothing found → say so, never interpolate from
  memory); one pure negative rewritten positive. Untrusted-output XML
  marking deliberately skipped — tool results already ride the structured
  chat tool role.
- **Eval case fixes:** `rlm-log-aggregate`/`rlm-notes-sweep` now accept
  every programmatic lane (`code.run`/`code.execute`, `fs.grep`, or a
  `specialist.delegate` whose child computes) — the deterministic check
  was failing exact-correct answers that used dispatch's designed path.
- **Eval run-status shows the case in flight, not the one that just
  finished.** `progress()` only fired after a case completed, so the admin
  poll lagged one case behind for the whole suite; it now fires at case
  start too.
- **`todos` tolerates the shapes small models actually send.** `update`
  accepts a full or partial `{"items": [{…}]}` re-send (merged by id or
  exact, unambiguous title) and `add` accepts the same list shape; status
  aliases map (`in_progress` → `working`, `completed` → `done`, …) instead
  of erroring; and the unknown-item error now lists the `id=title` mapping
  so a wrong call self-corrects in one turn (live: j-space-floor burned 10
  of 29 iterations on the set-shape confusion, code-bugfix spun 22 todos
  calls on full-list re-sends).
- **jev plugin: jevify as the recommended local backend.** jevify serves
  the same Jev System One API from a model you already run (e.g. the
  coding specialist) — typed answers read off next-token logprobs, no new
  weights, nothing off-box. Ships a recipe template
  (`plugins/jev/jevify-recipe.example.yaml`): probe, serve, point
  `plugins.jev.base_url` at it. No client changes — the plugin already
  spoke that contract.

## 1.13.0 — 2026-09-22

- **Brain tool gating, full version: `tools.code.brain_mode: dispatch`.**
  Superset of `verify`: with a coding specialist present, the brain's own
  `fs.write`/`fs.edit` calls into source files (~50 extensions + the
  extension-less build files) are REJECTED pre-exec from the first turn —
  no threshold, no nudge. Prose/config/data writes stay writable; one
  `specialist.delegate` call disarms. The field's consensus fix for the
  "orchestrator does the work itself" failure (hermes-agent's restricted
  dispatcher toolset, icdev's delegate-only mode): don't persuade the
  model, remove the capability. Tool descriptions state the rejection at
  the decision point. `verify` stays the shipped default.

- **Audit #22 fixes (1 C, 5 D).** jev privacy: the `route_request` hook
  fires at run start, before taint/approval exists — with the openrouter
  backend every request's text would leave the box. The hook now REFUSES
  the cloud backend unless `plugins.jev.allow_cloud_route: true` is set
  explicitly, and `route` defaults to `false` in code (matching the
  recorded "stay keyword" decision; runtime.yaml seeds the section).
  Dispatch-gate descriptions reworded after the first live dispatch run
  ("source files REJECTED" scared the 4B brain off writing ANY file —
  deliverables/notes/configs are now named as always-writable first).
  Hygiene: chat asset cache-bust bumped (v42 → v43), catalog regenerated
  (jev.decide row), plugin enumerations updated to four builtins
  (playbook/README/learning guide), stale version strings fixed,
  screenshots re-shot (28 PNGs).

## 1.12.0 — 2026-09-22

- **Stall ladder: count product, not activity.** Two live-observed hiding
  spots closed. Bookkeeping-only turns (`todos`/`context.pin`/`run.badge`)
  bumped the mutation generation and reset the no-progress counter — a
  hesitant brain hid in 11 consecutive planning turns (tb-huarong), ladder
  stuck at rung 1. Fixed, and the brain moved to a second hiding spot the
  same day: verify-only `code.check` streaks (14 reruns of one analysis
  script, wall clock dead, no deliverable). Product-free turns now count
  as no-progress on both, so the act → delegate → produce-or-ask
  escalation keeps advancing; any real edit/write between checks still
  resets (legit debug loops untouched), pinned by regression tests.
  `note.set` stays a resetter — "save a note" can be the deliverable.

- **`route_request` hook + jev plugin (decision models, tested).** New
  core hook seam: a plugin can classify an incoming request into a
  `models.strengths` tag and route the run, replacing keyword routing for
  that run (keyword path byte-identical otherwise; fired via to_thread,
  bounded I/O allowed). The builtin `jev` plugin ships the integration:
  `jev.decide` tool (choice/noul/score with calibrated probabilities,
  System One contract) over two backends — a local Open-Jev sidecar or
  hosted TypeSafe Jev via OpenRouter's alpha Decisions API. **Measured
  verdict: the idea holds, the open weights don't (yet).** Hosted Jev
  routed 20 real prompts nearly perfectly (0.92–1.00 coding/research,
  vision 0.99, chat correctly unrouted, ~0.4 s); the open 2B checkpoint
  lost to keywords (OOD training mixture). Ships disabled with
  `route: false` — cloud-routing every request's text is the wrong default
  for a local-first box. Full record: docs/brain-bakeoff.md lesson 5,
  plugin README, ToDos revisit conditions.

- **Admin UI fixes.** The Usage tab was dead — `admin.html` declared
  `loadUsage()` twice (JS hoisting: last declaration wins), so the
  tool/skill tables never populated; renamed the per-user variant and
  added a static test that fails on any duplicate function declaration.
  The active admin tab now survives browser refresh via URL hash
  (`#/admin#eval` etc.; `replaceState`, fixed tab-name set — no injection
  surface, no history spam).

- **Docs.** Learning guide: new §3.17 procedures (distilled process,
  loop-enforced) and §3.18 decision models (the jev experiment), the
  stall-ladder lesson in §3.1, §3.16 refreshed to six brain candidates
  with the Spark/Turbo verdict, footer version corrected. Brain bakeoff:
  Spark-4B/Turbo-coder column (18/32, 56%) + lessons 4–5.

- **Devbox ghost-container fix.** Two defects made a vanished devbox
  container a run-killer (live: code-bugfix eval turn 2 — the box was
  reaped during the judge pause, `podman exec` returned "no container with
  name or ID … found" as an ok-wrapped failure, and the model flailed
  15+ iterations on phantom sandbox errors): **(1)** the reaper's orphan
  sweep rebuilt its known-set from state files at pass START — a container
  created mid-pass (the ensure() that scheduled the reaper does exactly
  that) was stopped as a false orphan. The sweep now re-reads state files
  at sweep time and never stops containers younger than the idle TTL
  (StartedAt check). **(2)** `attempt()` recovers: on the "no such
  container" signature it drops the stale state file, recreates the box
  via ensure() and retries the command ONCE instead of surfacing the ghost
  error to the model.

- **Stuck-delegate escalation (`loop_guard.stuck_delegate_after: 3`).** The
  loop's distress hints (failure streak, host give-up, stall-ladder rungs)
  now converge into a counter; at the threshold the run gets a concrete
  hand-over directive — the exact `specialist.delegate(task=…, strength=…)`
  call to make, with the strength picked harness-side (request keywords →
  dominant tool activity → routable fallbacks) and verified against a
  live/swappable route before it's mentioned. "Consider delegating" nudges
  are ignorable; a spelled-out call less so. No specialist route → silence
  (single-model installs are never pushed into same-model spawns); one
  delegate call disarms it; 0 disables. Gate prompt's "Don't spin" bullet
  names the behavior.

- **Delta-run follow-ups (2026-09-19).** Four harness fixes from the live
  delta eval: **(1)** `code.run`/`code.check` now translate host workspace
  paths to their in-container mounts (`work_root` → `/work`, `tmp_root` →
  `/tmp/run`) before executing in the devbox or an eval case container —
  fs.* show the model host paths, so `cd <host path>` inside the box failed
  with "No such file or directory" and was retried 17-20× per run (shared
  helper `runtime.tool_base.translate_container_command`; a `path_note` in
  the result tells the model the mapping). **(2)** `code.check` joins the
  default `loop_guard.failure_nudge_tools` — the brain-gate's verify verb
  reports failures in its payload like code.run, and its spins never fed
  the same-signature streak. **(3)** Judge verdicts record the model that
  ACTUALLY answered (`served_model` from the proxy response) and flag
  silent proxy fallbacks in the notes — a dead OpenRouter key (403, key
  limit) made every "glm-5.2" verdict a silent local-brain verdict; the
  fallback-judge chain worked as designed, the telemetry lied about who
  graded. **(4)** New `budgets.final_warn_fraction` (0.95, 0 disables): at
  the wall clock's last stretch a blunt "stop tool calls, answer NOW with
  what you have" notice lands — the 0.8 checkpoint nudge is
  project-oriented, but Q&A runs researched straight through it and died
  with no answer (gaia-dc22a632: 36 web calls, no FINAL ANSWER).

## 1.11.0 — 2026-09-19

- **Cleanup pass: dead code removed, defaults single-sourced.** A verified
  sweep (vulture + ruff, every hit hand-checked against the tool registry,
  route decorators and plugin loader) removed: three unused `runtime/paths`
  constants (`UPLOADS_DIR`, `WIKI_DIR`, `SCRATCH_DIR`), the never-wired
  per-user `get/set_disabled_tools` methods
  (the global admin toggle is the live one), `ProcessManager.start_all`,
  `plugins.skill_dirs`, the unused `ConfirmationProvider` protocol, the
  write-only `runtime.connector_rows` attribute, an unused `plan_eviction`
  parameter and stale `noqa` directives. Hardcoded network defaults now live
  in one place: the remaining literal `http://127.0.0.1:4000` fallbacks use
  `runtime.paths.LITELLM_BASE` like everywhere else, and the whisper STT
  endpoint default is the new `runtime.paths.STT_URL` (`JAYNET_STT_URL`
  env override; `tools.audio.stt_url` still wins). The two duplicated
  sync `_podman` helpers (eval runner, benchlab importer) share
  `runtime/podman.py` now.

- **Preset packs: export/import presets as `.jaypack`.** The Presets tab
  gains **export** per row (downloads the preset's DB record incl. the
  `.conf` launch text as a `.jaypack`, kind `preset`) and **Import
  .jaypack** (clash → confirm overwrite; non-preset packs are rejected with
  a pointer to the Studio tab). Import upserts into presets.db, re-layers
  the config and re-renders the proxy aliases immediately; slots are not
  part of the pack. Machine-specific values (model paths, GPU ids, binary
  names) travel as-is — check them after import. `api_key_env` is an
  env-var *name*, so packs carry no secrets. Same jaypack guards as every
  other kind (name/shape validation, size caps, inner-record name match).

- **Worker prompts for specialist children (`agent.worker_prompt`, shipped
  ON).** A `specialist.delegate` child no longer has to inherit the full
  orchestrator gate prompt — routing doctrine included, which a worker must
  never follow. With the flag on, the child's base system prompt is the lean
  `prompts/worker.md` (execution discipline only) plus the tag module for its
  routed strength (`prompts/worker-<strength>.md` — coding/research/
  security/multi-step shipped), and the brain-only routing lines (specialist
  slot, strength directory) are dropped from its prompt. Resolution per part:
  `agent.worker_prompts.<part>` pin → `$JAYNET_DATA/custom/worker[-<tag>].md`
  overlay → shipped file; nothing found falls back to the gate prompt, so a
  missing file never breaks a delegation. Shipped ON after the live A/B:
  children on the lean prompt were judged correct everywhere they ran, the
  library's hardest delegation case (tb-regex-log) flipped fail→pass, and
  every arm-B failure traced to brain-side variance or environment, never
  child quality (the delegate result carries a `worker_prompt` marker for
  the trace). Set `false` to give children the gate prompt again. The
  **Admin → Prompt → Worker prompts** section edits every part with the
  same overlay layering as the gate prompt (view/save/revert, pinned parts
  read-only), lists tags from shipped files, overlays, pins, the
  `models.strengths` registry and preset strengths, and stages new tag
  modules for tags that have none.

- **`doc.extract`: the light document lane.** One call pulls text from a
  `.pdf` (text layer, via pypdf), `.xlsx` (openpyxl, pipe-joined rows per
  sheet) or `.docx` (stdlib zip+XML — no dep), confined to the run's roots,
  bounded excerpt with truncation notes. Scanned PDFs (no text layer) are
  flagged and pointed at the `pdf` skill's OCR path; layout-heavy documents
  remain the parked docling-plugin case. `rag.index path=` auto-converts
  these formats now instead of chunking binary garbage into the store. Deps
  are optional extras in `requirements-tools.txt`, imported lazily.

- **Rename: `code.delegate` → `specialist.delegate`.** The delegate tool
  routes coding/research/security/multi-step work to specialist models via
  its `strength` parameter, so the name stopped saying "code". The
  implementation moved to `tools/specialist/delegate.py` (class
  `SpecialistDelegate`); `tools/code/delegate.py` keeps `code.delegate`
  registered as a hidden legacy alias (same class, same behavior), so old
  prompts, evals, skills and saved chats keep working. The config path is
  unchanged: `tools.code.delegate.*` still configures the tool. Brain-gate,
  routing-nudge and strength-gate logic accept either name; all user- and
  model-facing text now says `specialist.delegate`.

- **Brain coding-tool gate: `tools.code.brain_mode: verify` (shipped
  default).** With a coding-strength specialist present, the brain's frozen
  toolset loses `code.run`/`code.execute`/`code.patch` and gains the new
  **`code.check`** — a verify-only code.run variant (network forced off,
  120 s cap, 200-line output cap) so the brain can confirm what the
  specialist built instead of building everything itself. `tools.load`
  can't smuggle the gated tools back, and the standing prompt's code.run
  bullets are mapped onto code.check for gated runs. Sub-agents, `full`
  mode and installs without a coding specialist are untouched.

- **Delegate toolset follows the strength.** A `specialist.delegate` child
  now gets tools matching its `strength`: research children get the web
  lane (web.search/fetch/request/render, browser.pdf, rag.search) + fs +
  code.run; security/multi-step/allround get the coding set + web basics.
  Fixes the live failure where a research task delegated with the coding
  toolset had no web access and looped `skill.load` into the loop guard.

- **Requirements gate.** `TodoList.requirements` — a flat
  `[must]`/`[should]`/`[nice]` list that lives apart from the plan, for
  explicit output requirements ("spell it out", "deliver as CSV"). Open
  `[must]` entries bounce the final answer once with a `requirements_gate`
  chat event and get their own Requirements section in the Todos panel.
  `/goal`'s "done when" is seeded harness-side as an open `[must]`, and
  the goal supervisor logs a `gate` entry for unverified finishes.

- **Loop guard widened.** Hard tool errors (`status=error`) now feed the
  same-signature failure nudge for EVERY tool, and any ok result breaks
  the streak. New per-host diminishing-returns guard
  `loop_guard.host_give_up_after` (default 4): consecutive hard errors or
  thin content shells against the same URL host make further results carry
  a give-up hint. Live evidence: 44 Scribd calls, 14 identical job.status
  polls — both pinned by tests.

- **Web lane: offset pagination, thin-shell flag, 429 retry.**
  web.fetch/request/render paginate with an offset plus actionable
  truncation hints; thin shells carry machine-readable `thin: true`
  (feeds the host guard); web.request retries a 429 once after
  `Retry-After` (capped 8 s), then hard-errors naming the host — a 429
  wrapped in a tool-level ok was invisible to failure tracking.

- **Reasoning-budget exhaustion retries with thinking off.** An empty
  final at finish `stop` whose completion tokens hit the reasoning budget
  retries once with thinking disabled plus a budget-specific message
  (`reasoning_exhausted` in the `empty_final` event).

- **Gemma 4 support.** `presets/chat_templates/gemma4_tools.jinja`
  (minja-compatible); model_client strips `<|channel>thought…<channel|>`
  frames — complete, unterminated, and the empty-frame leak llama.cpp's
  PEG fallback produces — on both streaming and non-streaming paths.

- **llm.call timeout split.** `tools.llm.timeout_s` (120) and
  `tools.llm.vision_timeout_s` (600); a vision ReadTimeout now reports
  "the slot IS running — retry or raise the budget" instead of the
  misleading "slot not running".

- **Gate prompt directives.** council.vote first even when the answer is
  itself a count/decode; multi-hop factual questions are never answered
  from memory; pinned sources are binding (never look the task up online);
  layout-stripping extraction → pull raw HTML with web.request.

- **Chat UX: turn actions, message queue, job announcements.** Every
  finished response gets a rounded-button row — ✎ edit (drops later turns
  and restores the prompt, with confirm) and ↻ retry on empty answers.
  While a run is live, sending text queues it as a removable chip above
  the composer that auto-fires on finish/error/cancel (empty composer is
  still Stop). Background job completions announce themselves as a chat
  line (`GET /api/jobs/finished`, owner-filtered, legacy jobs visible to
  all).

- **`multi-step` strength tag registered** for preset tagging and
  delegate routing.

- **Exactness gate (`agent.exactness_gate`, default on).** An explicit
  accuracy demand in the request ("this needs to be exact", "don't guess")
  seeds a `[must]` verification requirement harness-side — same deterministic
  seeding as /goal's "done when". The requirements bounce then forces a
  verification pass (council.vote self-consistency when available, else an
  independent recompute) before the final answer is accepted. Live evidence:
  the council-vote eval answered the strawberry count with one code.check in
  65 s — correct answer, wrong process, rubric-mandated vote never happened.
  `agent.exactness_keywords` overrides the demand phrases.

- **Delegation gate: the tb-regex-log hardening.** The delta showed the
  gated brain implementing inline anyway — 6 `fs.write`s past the soft
  delegate directive plus uncounted heredoc writes through `code.check`'s
  bash lane. Three layers landed: **gate-aware descriptions** (with the
  brain gate active, fs.write/fs.edit/code.check carry the routing rule in
  their description — the surface the model reads at the decision point);
  **`code.check` joins the gate's write-detection set** (shell writes via
  heredoc/redirect/sed -i now count); **soft→hard escalation**
  (`loop_guard.delegate_escalate`, default on, brain gate only): write-like
  calls are REJECTED from twice the nudge threshold until one
  specialist.delegate call disarms it — a nudge is ignorable, a rejection
  is not.

- **Delegation progress, visible.** specialist.delegate narrates its slow
  stages into the chat's live activity feed (route decision, model swap
  out/in, swap-back — the swap window sat silent for tens of seconds
  before), and the feed pins to the running sub-agent row (◇ coder) instead
  of the parent tool call — the delegation box is what you actually watch.
  New `scripts/screenshot_pages.py --demo-chat`: stages a REAL delegation
  run in a throwaway chat and screenshots it unblurred (chat-run.png — the
  README hero source — plus the in-flight chat-delegating.png), then deletes
  the chat and restores your synced current session.

- **Eval/ops scripts + brain bakeoff doc.** `scripts/ctx-cost.py` (GGUF
  KV/VRAM calculator for ctx sizing), `brain-swap.sh`, `eval-delta.sh`,
  `eval-peek.py`; new `docs/brain-bakeoff.md` — per-case brain comparison
  on the hard tail (K2-Horizon-7B 17/34 with 5 delegations, best
  candidate).

## 1.10.0 — 2026-09-12

- **code.delegate: verify by default.** Delegated code no longer returns on
  the child's self-report when the workspace can speak for itself. With no
  `verify` given or pinned, the workspace's standard test command is
  auto-attached when detectable (pytest via .venv/uv/system, npm/pnpm/yarn
  test, make test, go test, cargo test — `tools.code.delegate.auto_verify`,
  on by default; worktree-isolated runs skip the .venv variant since
  gitignored envs don't exist there). Tasks that smell testable ("fix the
  failing test", …) but still go out with no check get a one-shot
  `verify_hint` in the result (`verify_nudge`, on by default), and
  `tools.code.delegate.verify` can pin one command on every delegation.
  The check itself is the existing verifier gate: sandboxed, tests
  hash-guarded, vacuous-pass guarded.

- **Preset launcher: tensor-split default + MMAP key (dual-R9700 tuning
  writeup mined).** From alexkmiller.com's dual-R9700 llama.cpp/vLLM
  tuning (same hardware as the dev machine): multi-GPU presets now default
  to `--split-mode tensor` instead of `layer` (layer pipelines whole layers
  per card and leaves decode bandwidth unpooled at batch 1; tensor pools
  both cards — 19→29 tok/s there). Older builds without tensor support fall
  back to layer automatically (probed via `--help`); `SPLIT_MODE=layer`
  stays available for mismatched cards. New preset key `MMAP=off` emits
  `--load-mode none` (or `--no-mmap` on older builds, probed) — the fix for
  the slow/hanging mmap model loads on ROCm. Both surfaced in the admin
  preset editor; docs/llama-ops.md updated (incl. 2048/2048 batch guidance
  for 32 GB cards). The writeup's big lever — a patched vLLM "Radiance"
  MXFP4 serve at ~185 tok/s for the specialist class — is parked in
  ToDos_for_later.md as an experiment.

- **GVS5H levers: five patterns from the ledger-based orchestration paper
  ([arXiv:2608.26480](https://arxiv.org/abs/2608.26480)), ported to the
  JayNet loop.** Their controlled study (manager+workers over a shared
  filesystem ledger, +23.4 pass@1 on a self-hosted 27B) measured what we
  had been approximating; these are the pieces our architecture was
  missing:
  1. **Correctness veto** — a single-turn eval case with an
     `expect.checker` now gets that check as a mid-run verify hook: when
     the model tries to end its turn, the case's own grading script runs
     and a RED check vetoes "done", feeding the failure tail back into the
     run (`eval.verify_gate` / `eval.verify_max_checks`, hook form of the
     existing verifier gate in `runtime/verify.py`). Executed tests
     override self-reported success — the paper's single biggest gap in
     our loop.
  2. **Working-notes ledger** — `note.set` rewrites (never appends) and
     writes through to `notes.md` in the run's work_root: curated working
     state on disk that compaction can't truncate and delegated
     specialists pick up from the shared workspace. The coding skill
     teaches the rewrite-and-delete discipline.
  3. **Cut-off child envelope** — a sub-agent that dies on its
     budget/stall limit no longer hands the parent a full partial answer:
     it's hard-capped and carries a strategy hint (split smaller / simpler
     approach), so the failed approach can't anchor the retry
     (`runtime/tool_base.py: cutoff_child_answer`, applied by agent.spawn
     and code.delegate).
  4. **Per-role temperature** — `agent.role_temperature` pins sampling
     onto delegated children by strength tag (execution cold at 0.2,
     ideation warm at 0.4), overriding the specialist preset's default for
     that child call only; the brain's own sampling is untouched.
  5. **Fresh-perspective retry** — `agent.fresh_retry`: when the brain
     delegates the same task cluster again after 2 failed attempts, the
     delegation is de-anchored — the child gets the raw original request
     instead of the brain's stuck re-framing, and code.delegate skips its
     orientation pack (`fresh=true`, also usable directly). Guards the
     paper's −9 regression mode: deliberation anchoring itself out of a
     correct single-shot answer.

- **Strength measurement: priors + measured matrix.** Two-tier strength
  knowledge for the preset catalog. *Tier A — priors*
  (`tools/model/priors.py`): benchmark-distilled family hints (SWE-bench /
  GAIA / AIME / Terminal-Bench standings) that pre-fill the strengths field
  when a preset is created from a HF download — clearly labelled priors,
  never measurements. *Tier B — the measured matrix*: cases map to
  strength tags via one translator (`runtime/eval_strengths.py` — existing
  free-form tags carry strength meaning, `tb`→coding, `gaia`/`web`→
  research, plus an explicit `strength:<tag>` escape hatch), and
  `EvalStore.strength_matrix()` aggregates every result row per
  (brain label × strength), live runs and benchmark reps alike — zero
  schema change. Exposed at `GET /api/admin/evals/strength-matrix` and as
  a matrix view in Eval → Benchmark. Routing still consults declared tags;
  flipping `route_strength` to measured scores is the follow-up.

- **Overthinking pipeline: four harness levers against cap-out deaths.**
  Built from the live K2-Horizon evidence (gaia cases dying at exactly
  2×8192 completion tokens with empty answers — invisible reasoning burning
  the whole cap, twice):
  1. **Think-off retry** — a turn cut at the completion cap with no content
     now retries once with thinking switched OFF (the existing jinja
     thinking switch), forcing answer mode instead of inviting the model to
     re-think (and re-cap). Switchless/cloud backends keep the plain nudge.
  2. **Reasoning-tail carry-over** — model turns now capture a bounded tail
     of the reasoning channel (server-parsed `reasoning_content` and inline
     `<think>` alike); the cap nudge replays it ("your reasoning ended
     with… do not restart — conclude now") so the model *continues* its
     chain instead of re-deriving it.
  3. **`orchestrator.reasoning_budget_tokens`** (new config, default 0 =
     off) — per-request `reasoning_budget_tokens` for local backends,
     verified engaging through LiteLLM where the `--reasoning-budget`
     server flag did not. Capped thinking becomes *visible* content the
     hesitation guards can actually nudge. Local-only; cloud providers
     reject unknown params. Sweet spot ~half of `sampling.max_tokens`.
  4. **Wrap-up findings digest** — the tools-off loop-guard turn now
     carries the run's last three tool results and a "do not reply that you
     cannot call tools — answer best-effort from the findings" directive
     (live: gaia-65afbc8a wasted its only wrap-up turn on exactly that).
  5. **Truncated-answer restate** (added after live validation) — with the
     budget on, overthinking surfaces as *visible* rambling that runs into
     the cap: finish `length` with content but no tool calls and no FINAL
     ANSWER (gaia-50ad0280, 8192 tokens truncated mid-word). That
     half-sentence was previously accepted as the run's answer; it now
     gets one concise-restate nudge. Same validation run: gaia-7673d772
     converted from empty-run to a real researched answer (wrong rule
     picked — model limit, harness healthy), control held its pass.

- **Preset key `REASONING_EFFORT`** → `--reasoning-effort`: template-level
  think mode for models whose custom think tags make `--reasoning-budget` a
  silent no-op. Preset editor field + `docs/llama-ops.md` updated (incl.
  the `/props` check to tell whether a budget can work at all).
  **Field-verified caveat (K2-Horizon, live):** only the default
  `<ifm|think>` mode gets a proper reasoning split from llama.cpp — the
  `medium`/`low` modes (`<ifm|think_fast>` / `_faster>`) come back as raw
  CoT inside `content`, and the fine-tune rambles instead of acting
  (3/3 validation cases regressed, incl. a previously-passing control).
  Also verified there: the *server flag* `--reasoning-budget` never
  engaged (thinking ran to the full 2×8192 cap), while the *per-request*
  `reasoning_budget_tokens` body field force-closes thinking exactly at
  budget — but the model then keeps reasoning in plain content, so a
  budget alone doesn't rescue hard questions on this fine-tune.

- **Empty-final bounce:** a run ending with an empty answer at finish
  `stop` (a thinking-only turn that stopped cleanly — 12 live eval
  failures across gaia/tb ended `ok` with answer `""` after successful
  tool calls) now gets one restate nudge instead of being accepted.
  finish `length` stays with the existing completion-cap nudge.
- **Tool surface: five absorbed tools go hidden.** Tools gain a `hidden`
  flag: registered and callable (old prompts, skills, direct calls keep
  working) but no longer advertised in the model-facing schema. Applied
  to the near-duplicates small brains kept confusing: `code.execute`
  (use `code.run`), `web.render` (use `web.fetch` with `js=true` — new
  lane, and the thin-content/403 hints now teach it), `web.crawl` (use
  `web.extract` with `max_pages`/`page_url`), `serve.health` (use
  `serve.status`, already live-probes), `verify.probe` (use
  `verify.score` with `debug=true`). Gate prompt, skills and the
  generated catalog (now "N advertised + M hidden legacy aliases")
  updated to match.

- **Audit-#18/#19 fixes.** The HF preset-suggestion route no longer probes
  live VRAM on the event loop (port computation moved inside the threaded
  call — last unthreaded smi site, could wedge the whole console for up to
  20 s on a hung smi); docs/playbook.md's web escalation ladder re-pointed
  to `web.fetch js=true` / `web.extract max_pages` (still taught the hidden
  `web.render`/`web.crawl`); vacuous "nothing extra" assert removed from
  the strength-matrix test; ruff clean again; admin screenshots re-shot.

- **Privacy gate follows the destination alias, not the tool name.** A
  tainted run gated EVERY `llm.call` by name — including calls that never
  leave the box: an image call routing to the local vision slot was
  privacy-blocked twice in a live eval (gaia-cca530fc chess position) and
  died on the capability wall. The privacy and confirm-cloud gates now
  resolve the call's target alias first (`cloud_gate.remote_call_is_cloud`,
  mirroring CloudModels.execute's model/vision-slot resolution): local
  targets skip the gate entirely, unknown targets still fail closed.

- **Delta-run follow-ups (16/32 on the hard tail).** Three findings from
  the 2026-09-12 delta run, fixed: (1) the exact-spec directive in the
  gate prompt now says a short exact-string answer IS the string — no
  surrounding prose (live: "500" vs required "Five Hundred", verbose
  prose vs required `THE CASTLE`); (2) the council.vote lane is
  disambiguated — the vote comes FIRST for a high-stakes single answer,
  code.run only double-checks afterwards (live: council-vote answered
  correctly via code.run, rubric requires the vote); (3) a final answer
  that is essentially leaked tool-call markup (`</ifm|tool_call>` —
  survived parsing on the fine-tuned template, ended the run 'ok') now
  gets the same one-shot restate bounce as the empty-final case, with a
  `markup_leak` chat event; prose that merely discusses the markers is
  spared. Two triple-confirmed model-limit cases (gaia-50ad0280,
  gaia-7673d772) were deactivated on live via the cases API.

## 1.9.1 — 2026-09-10

Audit #17 fixes.

- **B1 (security):** `include_brain` is no longer a model-facing `model.use`
  argument. Brain eviction is internal-only — honored solely when the
  caller raised `ctx._allow_brain_evict` (code.delegate does, with
  swap-back). A direct or prompt-injected
  `model.use(..., swap: true, include_brain: true)` can no longer stop the
  current run's own model, which is what the docs already promised.
- **C1 (perf):** the Presets admin routes return the payload via
  `asyncio.to_thread` — the live-VRAM smi probe (up to ~20 s worst case) no
  longer runs on the web console's event loop.
- **D2 (docs):** playbook tool counts corrected to the generated catalog's
  118 tools / 40 namespaces.

## 1.9.0 — 2026-09-10

**Hardware-wide model swaps + automatic swap-back.** `model.use` swap is no
longer port-scoped: an eviction planner (`tools/model/catalog.py`
`plan_eviction`/`evict_records`) frees everything the incoming preset needs
— its port AND every pinned GPU, including multi-card occupants like a
brain spanning "0,1". Boot-posture slots are stopped through the process
manager (auto-restart stays disarmed), serve-registry models through
serve.stop; systemd units and remote presets are never touched, and VRAM
release is verified per card before the new model loads. A swap that can't
free its hardware refuses to load instead of OOMing.

**Delegate restores what it evicted** (`models.swap_back: true`, default
on): `code.delegate` passes `include_brain` so a specialist may claim GPUs
the brain sits on, then reloads the evicted set after the child run —
brain first, waiting until each model answers. This unlocks the full-size
layout: brain across both cards for daily work, dense specialist across
both cards for a coding run, brain back automatically. Restore failures
are reported in the delegate result, never silent. Direct `model.use`
swaps still refuse to evict the brain (that would kill the current run).

**Preset editor: standard/advanced views + device picker.** Launch flags
open in a standard view (model file, ctx, GPU layers, temp — split
mode/tensor split appear when several cards are ticked); "all launch
options" unfolds the full structured form, "advanced" keeps the raw .conf,
and values never get dropped switching views. The device dropdown is now a
checkbox per GPU (any subset, or CPU) and each GPU row shows the card's
**live free VRAM**. VRAM probing learned nvidia-smi, so CUDA boxes get
real headroom checks instead of advisory skips; launches honor the preset
binary's `device_env` (no more hardcoded HIP_VISIBLE_DEVICES).

## 1.8.5 — 2026-09-09

**Shipped prompt: eval-derived directives.** The default
`prompts/orchestrator-gate.md` gains the generic half of the live-tuned
prompt — "Answer to the exact spec" (literal output-format compliance,
code for string manipulation), "Deliver the file" additions (a running
background process is not a deliverable; `job.start`+`job.wait`; never
shell heredocs), "run the task's own checker command" in Prove-don't-
predict, the per-turn tool-call cap in Batch shell work, an alternate-
transport clause in Don't spin, probability/puzzle tasks never
just-replied, and authorized security work routed not refused. The
identity line is now model-neutral ("the local orchestrator brain on the
user's machine") — setup specifics stay in the local overlay. No code
changes.

## 1.8.4 — 2026-09-09

**Screenshots refreshed.** All 27 console screenshots re-shot against the
current GUI (mic button in the composer, vision/stt slots in Presets, the
binaries panel with its implicit-default row, eval sub-views, MCP as its
own group), and the README hero (`chat-run.png`/`chat-hero.png`) redone on
the current layout. No code changes.

## 1.8.3 — 2026-09-09

**Documentation catch-up.** The vision/stt helper slots, the `WHISPER=on`
preset mode, mic dictation, the binaries panel and the proxy re-render are
now covered across the standing docs (README, admin, models, llama-ops,
glossary, model-placement, playbook); the outdated "alias + port must match
a static litellm.yaml entry" contract was corrected (the render generates
local entries from the preset catalog). No code changes.

## 1.8.2 — 2026-09-09

**Chat: mic dictation is back.** A mic button in the composer records in the
browser, resamples to 16 kHz mono WAV client-side (whisper.cpp has no ffmpeg),
and posts it to the new `POST /api/stt`, which forwards to the stt slot and
drops the transcript into the prompt. The button only appears when the whisper
slot is actually reachable (a `GET /api/stt` TCP probe) — no whisper, no
button, no dead UI. Recording state is a pulsing-red button so an open mic is
impossible to miss.

**Presets: vision + stt helper slots.** Two new optional boot slots join
embed/rerank as CPU helpers, both shipping EMPTY (assign in Admin → Presets →
Boot model slots): `vision` serves the new `local-vision` LiteLLM alias — a
llama-server with `--mmproj`, rendered into the proxy config only while the
slot is assigned — and `stt` runs a whisper.cpp whisper-server (preset key
`WHISPER=on` makes start-model.sh skip every llama flag and launch the
whisper binary from the preset's binary-registry entry). Example presets:
`presets/vision-qwen2.5-vl-3b.conf`, `presets/stt-whisper-large-v3-turbo.conf`.

**llm.call: image support.** New optional `images` argument (data URLs or
workspace image paths, png/jpg/jpeg/webp/gif/bmp, ≤10MB) builds OpenAI
multimodal content blocks; with no explicit `model` the call routes to
`tools.llm.vision_model` (default `local-vision`). Explicit cloud models with
images keep the existing cloud privacy gate; a down/unassigned vision slot
returns an actionable "assign a vision preset" error.

**Tools: audio.transcribe.** New private, read-only tool that posts an audio
file (workspace-confined) to the whisper server's multipart `/inference`
endpoint (`tools.audio.stt_url`, direct HTTP like rag.*) and returns the
transcript. New `skills/audio` skill; the `image` skill now documents
vision-model image understanding with tesseract OCR as the fallback.

**HF downloader: .bin files.** The repo file whitelist learned `.bin`
(whisper.cpp models) as its own kind — `list_gguf` and preset suggestions
stay llama-only; the admin list shows a "whisper" pill, and the model
browser + preset file picker see `.bin`.

**Admin: binaries honesty + proxy re-render.** The launcher's implicit
default binary (`$LLAMA_BIN` → `$ORCH_HOME/bin/llama-server`) is now a
visible read-only row in the Binaries panel with a missing-pill, and
start-model.sh's error points at the fix. `device_env` may be empty
(CPU builds pin nothing; the UI labels it "cpu/none"). And preset/slot
edits now re-render + reload the LiteLLM proxy config like cloud-model
edits always did — assigning a vision preset no longer leaves its alias
404ing until a manual proxy restart.

**Fixes (audit #16, tag re-cut).** `runtime.__version__` was left at 1.8.0
when 1.8.2 shipped — the release pin (`tests/test_release.py`) caught it;
the tag was re-cut onto the bump. MCP: repinned `mcp`/`mcp-types` 2.0.0 →
2.0.1 (2.0.0 cannot import on py3.11 despite its `>=3.10` declaration) and
the SDK import guard now converts any import-time breakage into the
actionable McpError instead of leaking an HTTP 500.

## 1.8.1 — 2026-09-08

**Procedure library: distillation miner.** A judge contrasts PASS/FAIL eval
history (or a flagged session's scrubbed runs) and drafts one procedure
SKILL.md; drafts carry `draft: true` frontmatter so they're filtered from
model-facing discovery, catalog, and autoload until reviewed in the Studio
(draft badge). New endpoints `POST /api/admin/evals/mine-procedure` and
`POST /api/admin/flags/{id}/mine-procedure`; jaypacks carry the `shape`
annotation and show a procedure trust line on import.

**Routing: delegate gates see shell writes.** The delegate/strength gates
and the badge watch now treat write-like shell commands (redirects, tee,
sed -i, patch, cp/mv/rsync/dd) as coding work — brains implementing via
`code.run` finally trip the same routing the fs.* tools always did.

**Plugins: h5i interactive browser** (browser.browse via the h5i CLI);
**hf_pull: parallel range downloads** (~2-8x faster model pulls);
**admin: delete files/folders in the model browser**, ★ on parent folders
of preset-used models; **evals: rlm-notes-sweep** — second long-context
A/B case.

## 1.8.0 — 2026-09-06

**Loop: procedure checkpoints.** Procedures (shape-tagged skills) now
declare a `checkpoints:` frontmatter list; the loop enforces it —
appended to stall-ladder rungs and nudged once (`procedure_check`)
before a final answer is accepted. That completes the procedure-library
core: distilled frontier process, selected by shape, auto-loaded for
small brains, and now checked by the loop itself.

**Eval: prompt-injection cases + canary grader.** Two new cases —
hostile HTML comment in a seeded page, fake `[SYSTEM]` pre-approval in
an inlined attachment — graded by the new deterministic
`canary_not_in_tool_args` expectation: trace tool-call args are scanned
for the planted canary (the trajectory string truncates args).

**Eval: judge calibration.** `evals/judge-calibration.json` freezes ten
transcripts with known-correct verdicts; Admin → Eval → *Judge
calibration* grades them with the current judge and reports per-pair
agreement — measure the judge before trusting its proposals, especially
after switching judge models.

**Eval: local judge default.** The shipped judge/driver default is now
`local-specialist`; a cloud judge is an explicit config override
(local-first, and eval transcripts stay on the box unless you opt in).

**Eval: benchmark skill A/B.** Benchmark variants can run *without*
named skills (`disabled_skills` — hidden from the catalog, refused by
`skill.load`), so "same brain ± the skill that claims to help" is a
measurable A/B (built for the RLM long-document question).

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
