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
channel-markup fix).

**Lessons so far.**

1. Sub-5B-active brains can't hold standing instructions under load
   (ask.user/skill.load/format discipline all regress). The brain needs mass.
2. The delegation tripwire works when the model is willing (Gemma: 5/30,
   two passes via the specialist) and is ignored when it isn't (Ling: 0
   voluntary delegations, two context blowups). Delegation count is a
   better brain-health metric than raw pass rate.
3. K2 stays the reference brain: fewer passes than Ornith-era numbers on
   this set, but it routes reliably and survives the chain cases.

| case | Ornith era | K2 era | Ling 7.9B/A1.3B | Gemma-4 19B/A4B |
|---|---|---|---|---|
| ask-user | 17/20 | 1/1 | f | f |
| code-spec-conflict-trap | 12/20 | 1/5 | f | f |
| council-vote | 0/13 | 1/7 | f | f |
| fs-roundtrip | 15/19 | 0/0 | — | **P** |
| gaia-11af4e1a | 7/10 | 0/0 | — | **P** |
| gaia-23dd907f | 0/11 | 3/6 | f | f |
| gaia-27d5d136 | 9/10 | 0/0 | — | f |
| gaia-2d83110e | 3/11 | 3/4 | — | **P** |
| gaia-389793a7 | 9/10 | 0/0 | **P** | — |
| gaia-3cef3a44 | 2/11 | 4/5 | f | f |
| gaia-46719c30 | 2/11 | 2/5 | f | f |
| gaia-4b650a35 | 1/11 | 4/5 | **P** | — |
| gaia-4b6bb5f7 | 2/11 | 0/7 | f | f |
| gaia-4fc2f1ae | 9/10 | 2/3 | f | **P** |
| gaia-50ad0280 | 5/11 | 0/10 | f | f |
| gaia-65afbc8a | 2/10 | 1/6 | f | f·deleg |
| gaia-7673d772 | 0/10 | 0/9 | f | f |
| gaia-7d4a7d1d | 3/10 | 2/5 | f | f |
| gaia-9318445f | 0/5 | 1/9 | f | f·deleg |
| gaia-935e2cff | 7/10 | 5/9 | f | **P**·deleg |
| gaia-99c9cc74 | 2/5 | 3/4 | f | **P**·deleg |
| gaia-b816bfce | 8/9 | 1/1 | **P** | — |
| gaia-bda648d7 | 7/10 | 3/4 | — | f |
| gaia-c365c1c7 | 0/10 | 0/8 | f | f |
| gaia-cabe07ed | 7/10 | 2/5 | f | f·deleg |
| gaia-cca530fc | 0/10 | 0/7 | f·deleg | f |
| gaia-d0633230 | 1/10 | 3/6 | f | f |
| gaia-dc22a632 | 7/10 | 0/8 | f | f |
| gaia-e142056d | 0/10 | 0/5 | f | f |
| j-space-floor | 17/18 | 2/3 | f·deleg | f |
| rlm-notes-sweep | 0/0 | 4/5 | **P** | — |
| skill-load | 6/20 | 4/5 | — | f |
| web-fetch-lane | 17/18 | 2/2 | f | f |
| web-freshness | 15/18 | 0/0 | **P** | f |
| **total** | 192/392 | 54/159 | **5/28** (deleg 2) | **6/30** (deleg 5) |
