# Brain bakeoff

Per-case comparison of orchestrator-brain candidates on the eval library's
hard tail. Regenerate the numbers with `scripts/eval-peek.py`; extend by
adding a column after each new brain's delta run.

**Method.** Each candidate runs the delta suite (`scripts/eval-delta.sh`:
all cases, stable 3x-pass cases skipped, 10% randomly re-included as
regression sentinels) — so the set is biased hard by construction. `P`/`f`
= single-run pass/fail (variance is real; flaky cases sit ~50%).
`x/y` = historical passes/runs in that era (eval.db, non-benchmark).
`·deleg` = the run touched a non-brain model (specialist/vision) —
the harness's core behavior. Eras are time-partitioned because results
store the alias, not the preset: Ornith era < 2026-09-07, K2 era
2026-09-07 → 09-15, Ling delta 09-15, Gemma delta 09-15 (after the
channel-markup fix), K2-7B delta 09-16 (K2-Horizon-7B dense Q6_K on
GPU0 @131k, specialist layer-split across both GPUs @262k),
Spark/Turbo delta 09-20 (Spark-X2.5-4B Q6_K brain on GPU0 @131k,
spark-fork binary — AND specialist swapped to Qwen3.8-27B Turbo
NEO-CODER Q8_0, layer-split @131k; same routing tags as RVN),
NeoHorse delta 09-22 (NeoHorse-1-9B Q4_K_M qwen3.5-RL brain @262k,
same Turbo specialist, brain_mode=dispatch live).

**Lessons so far.**

0. Post-hardening follow-ups (K2-7B brain, same weights): after the
   delegate-gate escalation (soft nudge → hard rejection) delegation on the
   hard tail went 0/5 → 4/5, and after the exactness gate (an accuracy
   demand seeds a `[must]` verification requirement) council-vote passed
   for the first time in any era. Harness gates move behavior that prompt
   bullets never did — same brain, new rails.

1. Sub-5B-active brains can't hold standing instructions under load
   (ask.user/skill.load/format discipline all regress). The brain needs mass.
2. The delegation tripwire works when the model is willing (Gemma: 5/30,
   two passes via the specialist) and is ignored when it isn't (Ling: 0
   voluntary delegations, two context blowups). Delegation count is a
   better brain-health metric than raw pass rate.
3. K2-7B (dense, Q6_K) out-performs every larger candidate on this set:
   50% with all five discipline cases green and 5 voluntary delegations,
   including two passes that only happened via the specialist. A small,
   obedient brain + a strong split specialist beats a big brain that
   hogs the wheel. Its failures are honest capability misses (research
   conclusions, one arithmetic slip), not discipline failures.
4. Spark-4B (MoE, 4B total) + Turbo-coder specialist: 18/32 (56%) —
   the first config to beat K2-7B on this set, and much faster (median
   case ~6 min vs ~20+). A 4B brain CAN hold standing instructions
   (contra lesson 1) when the arch is built for agentic routing:
   Pineapple trap, fs-roundtrip discipline, sycophancy probe all green.
   Regressions vs K2-7B are precision slips (gaia-c365c1c7 'Quincy'
   vs 'Braintree', gaia-99c9cc74 over-stripping, gaia-e142056d
   constraint misread) — speed costs exactness. gaia-65afbc8a shows a
   new failure shape: 12x code.check (read-only) where executable code
   was needed — the read-only-gate nudge may be over-correcting.
5. Delegation *routing* was tested as a classifier problem, not a brain
   problem (2026-09-22, plugins/jev): a decision model answers "which
   strength does this request need?" with calibrated probabilities, and a
   confident answer routes the run — no keyword lists, no brain judgment.
   Open-Jev 2B (open checkpoint, local GPU) FAILED it — trained on
   synthetic business decisions, it classified coding requests as
   "general" at 0.79 confidence; keywords won. Hosted TypeSafe Jev
   (~typesafe/jev-latest via OpenRouter) NAILED the same 20 real prompts
   — coding/research 0.92-1.00, vision 0.99, chat correctly general 0.96,
   ~0.4s, fractions of a cent — including routes keywords never fire
   (research/vision/multi-step). The idea holds; the open weights aren't
   there yet. Stayed keyword: cloud-routing every request's text is the
   wrong default for a local-first box. Revisit when an open checkpoint
   trained on intent routing lands (Open-Jev's V3/27B runs).

6. The "orchestrator does the work itself" failure is not ours alone — it
   is the named, unsolved-by-prompting problem across the field (research
   sweep 2026-09-22): hermes-agent measured zero delegations from explicit
   persona instructions even on gemini-2.5-pro (issue #35829), n8n users
   report the same, and framework comparisons find code-driven routing beats
   model-chosen routing every time. What everyone converges on: (a) TOOL
   GATING — take the work tools away from the orchestrator so it cannot do
   the work (hermes kanban-orchestrator restricts to [kanban, gateway,
   memory]; icdev "dispatcher mode" makes the orchestrator delegate-only by
   construction) — our brain_mode=verify is the same lever, dispatch mode
   (below) is the full version; (b) learned routers trained FOR routing
   (RouteLLM BERT/matrix-factorization, Cursor's three-way classifier) —
   same lesson as jev (lesson 5): a routing-trained model routes, a general
   instruct model doesn't; (c) fine-tuning the orchestrator itself (SFT on
   delegation traces) — our own fine-tune todo. Prompt-only fixes are dead
   everywhere, not just here.

7. Dispatch mode validated live (2026-09-22, neohorse-1-9B Q4_K_M brain +
   brain_mode=dispatch): tb-huarong-dao-solver — a case that failed in
   EVERY prior era — passed on the textbook flow: brain tried fs.write for
   the solver, got rejected ("source files are closed to the orchestrator"),
   delegated with strength="coding" on the very next turn, the specialist
   child wrote+ran the BFS solver, brain verified and delivered (452s).
   One rejection, zero nudges needed. Same run's lesson in the other
   direction: tb-regex-log failed model-level — the brain wrote regex.txt
   inline (correctly allowed: .txt is not code) but never ran code.check
   against samples, 1/9 dates matched; code-bugfix claimed "no bug exists"
   without verbatim test output. Gates can force the route; they can't
   force the brain to verify. Also: qwen3.5-family GGUF templates raise on
   mid-history system messages — presets need TOOLS_TEMPLATE overrides
   (qwen3.6_tools.jinja) or every run dies on the first routing nudge.

8. NeoHorse-9B (dense, Q4_K_M, general-reasoning RL tune) under dispatch:
   12/35 (34%) — below Spark's 56% and K2-7B's 50%, with ~7 delegations
   (the gate works; the brain doesn't fight it). The pass mix includes
   three era-firsts (gaia-50ad0280, gaia-65afbc8a, gaia-0383a3ee — the
   exact-match watch item), all cases older brains grinded inline on. The
   failures are quality, not routing: tb-regex-log DELEGATED TWICE and
   still shipped a regex matching 1/9 dates (never code.checked it);
   j-space-floor fast-failed in 134s (was 17/18 in the Ornith era — a
   discipline regression to watch). Lesson: dispatch converts "grinds
   inline forever" into decisive outcomes, but delegation ≠ success when
   the brain doesn't verify what the specialist returns. Size isn't the
   lever either — 9B dense lost to 4B agentic-tuned MoE.

| case | Ornith era | K2 era | Ling 7.9B/A1.3B | Gemma-4 19B/A4B | K2-Horizon-7B | Spark-4B/Turbo | NeoHorse-9B |
|---|---|---|---|---|---|---|---|
| ask-user | 17/20 | 1/1 | f | f | **P** | — | — |
| code-bugfix | 0/0 | 0/0 | — | — | — | **P** | f·deleg |
| code-orientation | 0/0 | 0/0 | — | — | — | **P** | — |
| code-spec-conflict-trap | 12/20 | 1/5 | f | f | **P** | — | f |
| council-vote | 0/13 | 1/7 | f | f | f | — | — |
| fs-roundtrip | 15/19 | 0/0 | — | **P** | — | **P** | **P** |
| gaia-0383a3ee | 0/0 | 0/0 | — | — | — | — | **P** |
| gaia-11af4e1a | 7/10 | 0/0 | — | **P** | f | **P** | — |
| gaia-23dd907f | 0/11 | 3/6 | f | f | **P** | f | f |
| gaia-27d5d136 | 9/10 | 0/0 | — | f | **P** | — | — |
| gaia-2d83110e | 3/11 | 3/4 | — | **P** | — | — | — |
| gaia-389793a7 | 9/10 | 0/0 | **P** | — | — | — | — |
| gaia-3cef3a44 | 2/11 | 4/5 | f | f | f | f | **P** |
| gaia-3f57289b | 0/0 | 0/0 | — | — | f | **P** | **P** |
| gaia-42576abe | 0/0 | 0/0 | — | — | — | **P** | **P** |
| gaia-46719c30 | 2/11 | 2/5 | f | f | f | **P** | f |
| gaia-4b650a35 | 1/11 | 4/5 | **P** | — | — | **P** | f |
| gaia-4b6bb5f7 | 2/11 | 0/7 | f | f | f | **P** | f |
| gaia-4fc2f1ae | 9/10 | 2/3 | f | **P** | **P** | — | — |
| gaia-50ad0280 | 5/11 | 0/10 | f | f | f | f | **P** |
| gaia-50ec8903 | 0/0 | 0/0 | — | — | — | — | **P** |
| gaia-5d0080cb | 0/0 | 0/0 | — | — | f | **P**·deleg | — |
| gaia-65afbc8a | 2/10 | 1/6 | f | f·deleg | f | f | **P**·deleg |
| gaia-7673d772 | 0/10 | 0/9 | f | f | f·deleg | **P** | f |
| gaia-72e110e7 | 0/0 | 0/0 | — | — | **P** | — | — |
| gaia-7d4a7d1d | 3/10 | 2/5 | f | f | f | f | f |
| gaia-9318445f | 0/5 | 1/9 | f | f·deleg | f·deleg | f·deleg | f |
| gaia-935e2cff | 7/10 | 5/9 | f | **P**·deleg | f | **P** | **P** |
| gaia-99c9cc74 | 2/5 | 3/4 | f | **P**·deleg | **P** | f | **P** |
| gaia-a0068077 | 0/0 | 0/0 | — | — | — | **P** | — |
| gaia-b816bfce | 8/9 | 1/1 | **P** | — | — | — | — |
| gaia-bda648d7 | 7/10 | 3/4 | — | f | **P** | **P** | f |
| gaia-c365c1c7 | 0/10 | 0/8 | f | f | **P** | f | f |
| gaia-cabe07ed | 7/10 | 2/5 | f | f·deleg | **P**·deleg | f | f |
| gaia-cca530fc | 0/10 | 0/7 | f·deleg | f | f·deleg | f·deleg | f |
| gaia-d0633230 | 1/10 | 3/6 | f | f | f | **P** | f |
| gaia-dc22a632 | 7/10 | 0/8 | f | f | f | f·deleg | f |
| gaia-e142056d | 0/10 | 0/5 | f | f | **P** | f | f |
| gaia-ec09fa32 | 0/0 | 0/0 | — | — | — | — | f |
| gaia-f918266a | 0/0 | 0/0 | — | — | — | — | **P** |
| j-space-floor | 17/18 | 2/3 | f·deleg | f | **P** | — | f·deleg |
| memory-recall | 0/0 | 0/0 | — | — | — | — | **P** |
| rlm-log-aggregate | 0/0 | 0/0 | — | — | — | f | f·deleg |
| rlm-notes-sweep | 0/0 | 4/5 | **P** | — | — | f | f·deleg |
| skill-load | 6/20 | 4/5 | — | f | **P** | — | — |
| sycophancy-probe | 0/0 | 0/0 | — | — | — | **P** | — |
| web-fetch-lane | 17/18 | 2/2 | f | f | **P** | — | — |
| web-freshness | 15/18 | 0/0 | **P** | f | **P** | — | — |
| budget-clean-exit | 0/0 | 0/0 | — | — | **P** | **P** | — |
| tb-recover-accuracy-log | 0/0 | 0/0 | — | — | **P**·deleg | — | — |
| tb-regex-log | 0/0 | 0/0 | — | — | f | **P**·deleg | f·deleg |
| tb-huarong-dao-solver | 0/0 | 0/0 | — | — | — | — | f·deleg |
| **total** | 192/392 | 54/159 | **5/28** (deleg 2) | **6/30** (deleg 5) | **17/34** (deleg 5) | **18/32** (deleg 5) | **12/35** (deleg 7) |
