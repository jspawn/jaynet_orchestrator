# Brain bakeoff

Per-case comparison of orchestrator-brain candidates on the eval library's
hard tail. Regenerate the numbers with `scripts/eval-peek.py`; extend by
adding a column after each new brain's delta run.

**Reading the columns.** Each column is an unpaired single-rep run over a
case-biased set — 18/32 carries a 95% Wilson interval of roughly 39–72%,
and neighbouring columns overlap heavily. Differences inside overlapping
intervals are noise, not signal: never rank two brains on raw columns.
For an actual call, pair the latest result per case with
`scripts/eval-peek.py --compare A B` (McNemar exact on discordant pairs).

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
Spark@0.75+gate delta 09-23 (same sharp Spark Q6_K brain, temp
0.6→0.75 A/B + just-reply gate live; same Turbo specialist @262k).
Spark@0.75+v1.14.0 delta 09-24 (identical brain/specialist/sampling
as the previous column; harness v1.14.0: guard pipeline, stall
hard-stop, repeat-error hard-block, specialist-authored checks,
plain-text fs returns + forgiving fs.edit).
Bonsai-27B delta 09-28 (Ternary-Bonsai-2-27B-Abliterated-v2
PQ2_0-MTP brain, single GPU0 @262k q8 KV, Q8 mmproj vision via
pinned MEDIA_MARKER, qwen3.6 tools template; same Turbo
specialist @262k, tensor 1,4; 41 cases = 31 harness + 10 TB,
GAIA excluded to spare searXNG).
qwen35-9B delta 09-28/29 (DavidAU Qwen3.5-9B plusIQ-TOOLS
NEO-MAX-MTP Q6_K — first qwen35-arch brain, text-only (no mtmd
support yet), @262k q8 KV, single GPU0; same Turbo specialist;
delegate child cap raised 24 -> 32 mid-run).

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
   Follow-on (2026-09-27, plugins/clm): CLM v0.1 (contrastive decision
   model, local CPU) was benched as that open-checkpoint candidate on
   243 labeled real requests — top-1 18.9% vs keyword 13.2%, research
   0% (reasoning prior, p=0.96 on stripped GAIA), p50 2.6s vs the 2.0s
   hook budget. Same verdict shape as Open-Jev: not routing-trained
   enough. Hosted Jev on the SAME set: 73.3% top-1 at 0.3s flat —
   the idea confirmed at scale; the local checkpoints are what lag.
   Both hooks stay off (CLM too weak, Jev is cloud). clm.decide/
   clm.rank stay as tools. jevify (local specialist as decision
   backend) is the open fourth column. Full numbers:
   docs/clm-bakeoff.md.

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

9. Verify-the-delegate bounce validated live (2026-09-22, Spark-4B "sharp"
   temp-1 variant as brain, brain_mode=dispatch + verify_delegate_check):
   code-bugfix — the dispatch gate rejected the brain's fs.write to calc.py
   twice on turn 1, it delegated with strength="coding" on turn 2, and ran
   code.check AFTER the delegation, so the bounce correctly stayed silent;
   green run, pass (627s). tb-regex-log — the brain wrote regex.txt inline
   (correctly allowed: not a source file), then spun on 14 code.check calls
   for 47 min without ever delegating, ignoring all three stall rungs;
   verify_check correctly never fired (no delegation happened). Net: both
   gates behave exactly as designed under Spark — the remaining failure is
   model-level stubbornness, not harness. Note Spark passed tb-regex-log
   WITH delegation in its full delta, so this is run-to-run variance, not
   a dispatch regression.

10. Temp A/B, Spark-4B (2026-09-23, same sharp Q6_K + Turbo specialist,
    brain_mode=dispatch): 0.75 vs the previous night's 0.6 column —
    15/39 (38%) vs 11/36 (31%); on the 34 shared cases 6 F→P vs 4 P→F,
    McNemar p=0.754 — **no significant difference**; the point estimate
    is noise. What IS signal: (a) the 0.6 just-reply obedience cluster
    (web-fetch-lane fetching Melville then answering Kipling) recovered
    at 0.75 — web-fetch-lane F→P; (b) the just-reply gate fired only 3×
    in 39 cases and converted 0 — all three were comprehension puzzles
    where no tool genuinely applies (fictional-language role mapping,
    antonym-vs-reversal, ignored meta-instruction); the gate is cheap
    and harmless but this set can't show its value; (c) the remaining
    24 fails are model-level (comprehension, wrong-entity recall) with
    8.7h of fail-side burn — the exact class the same-day enforcement
    rails target (stall hard-stop after the final rung, repeat-error
    hard-block, specialist-authored checks). tb-regex-log failed a
    4th consecutive dispatch-era run (2,443s) — firmly model-limit now.
    Decision: keep 0.75 (recovered exploration, no measured cost); the
    0.3/0.75/1.0 ladder on the fixed list is the real test.

11. Harness-batch A/B (2026-09-24, v1.14.0, SAME brain + specialist +
    temp 0.75 as the previous column — the question was "did the rails
    help the same brain", not a brain comparison): 19/39 (49%) vs
    15/39 (38%); 9 F→P vs 5 P→F, McNemar p=0.21 — direction positive,
    still under significance on 39 cases. The unambiguous win is burn:
    fail-side elapsed on the 15 persistent fails dropped 19,599s →
    14,553s (-26%), led by gaia-e142056d 2,504→502s (-80%, stall
    hard-stop killed the 26× code.check loop at turn 6) and
    gaia-46719c30 1,838→287s (-84%). tb-regex-log finally passed
    (f·deleg → **P**·deleg, 2,021s) after 4 consecutive dispatch-era
    fails — the authored-check rail (13 delegations returned with a
    specialist-written check, 12 verified green, 1 correctly caught a
    bad delegation) is the likely mechanism. P→F flips classified:
    code-bugfix = the pre-existing stale-devbox-container race
    (`no such container` on the pytest code.check; happened 39× in the
    morning column too — NOT a v1.14.0 regression, host-fallback fix
    pending); rlm-notes-sweep + web-fetch-lane + gaia-f918266a +
    gaia-d0633230 = model-level variance/extraction errors.
    repeat_blocked never fired (0×) — the hard-stop at the stall
    rungs intercepts loops before identical-error repeats hit 3.
    Remaining model-level wall: comprehension puzzles (gaia-50ad0280,
    gaia-50ec8903) where the brain still reaches for code.check
    (read-only) instead of computing.

12. Taichu-9B full sweep (2026-09-24/25, v1.14.2, brain ZDTaichu5.0-9B
    Q8_0 @ temp 0.7, CTX 131k, specialist Qwen3.8-27B Turbo — first
    FULL 89-case sweep; earlier columns are subsets, so compare
    per-case, not the totals): 38/89 (43%) = harness 18/31 (58%),
    GAIA 16/48 (33%), TB 4/10. Wall 4h48m. First-ever
    tb-huarong-dao-solver pass (1,606s, delegated, after 5 consecutive
    fails across three brains) and code-bugfix green again after two
    dispatch-era fails. Strongest GAIA showing yet, incl. first-time
    solves gaia-27d5d136 (logic), gaia-5cfb274c (Hamiltonian cycle),
    gaia-c714ab3a (vampire puzzle), gaia-dc28cf18 (family tree),
    gaia-cffe0e32 (DOCX parse). Regression flips vs Spark@0.75+v1.14.0:
    code-spec-conflict-trap (silently rewrote pricing.py — the trap
    keeps catching "helpful" brains), rlm-log-aggregate (counts right,
    but 19 iterations over the 10-cap in a code.symbols loop),
    web-fetch-lane (hallucinated the fetched page's content),
    gaia-2d83110e (string reversal), gaia-99c9cc74 (alphabetizing),
    gaia-ec09fa32 (answered from memory, no simulation),
    tb-recover-obfuscated-files. Failure-mass classification: 4 GAIA
    fails were pure infrastructure (tavily HTTP 432 on every search),
    2 were vision-slot misreads (gaia-9318445f, gaia-cca530fc — not the
    brain), the chronic cluster (j-space-loop, gaia-23dd907f,
    gaia-c365c1c7, gaia-46719c30) is unchanged across ALL brains.
    Delegation review (v1.14.2) live-fired 29×: 20 pass / 9 fail, and
    the fail verdicts were substantive (fabricated verification
    output, 6-letter answer to a 7-letter clue, unverified web
    claims) — the review rail works regardless of which brain drives.

13. Spark@f16/261k full sweep (2026-09-25, same v1.14.2 harness as the
    Taichu column — brain Spark-X2.5-4B Q8_0, f16 KV, CTX 262144, temp
    0.75, tensor 5,5, specialist Qwen3.8-27B Turbo): 52/89 (58%) =
    harness 25/31 (81%), GAIA 18/48 (38%), TB 9/10 — best on every
    axis vs Taichu (38/89 = 18/31, 16/48, 4/10). 21 F→P vs 7 P→F,
    McNemar chi2=7.0, p~0.008 — significant. Wall 9h24m vs Taichu's
    4h48m: the price of reviving hard cases — 9 cases burned >1000s
    (gaia-50ec8903 2,597s, gaia-7673d772 2,497s, both still red).
    Chronic breakers (failed in EVERY prior column): j-space-loop,
    gaia-23dd907f, gaia-389793a7, gaia-9d191bce (YouTube), and
    tb-regex-log — Spark's own historical todos-loop case, clean in
    463s at this config. tb-huarong-dao-solver solved 2.6x faster
    than Taichu (625s vs 1,606s). Negative flips (7): code-feature-spec,
    gaia-27d5d136, gaia-3cef3a44, gaia-50ec8903, gaia-cf106601,
    gaia-cffe0e32, gaia-dc28cf18 — precision/comprehension tasks where
    the 9B's extra weights earn their keep. Delegation review: 50
    reviews, 28 pass / 21 fail / 1 unclear — fail rate 2x Taichu's
    (Spark's reports carry thinner evidence; the reviewer catches it),
    yet the warnings still drove productive re-verification in this
    config — even with the OLD warning text (57ed4fb not yet deployed).
    Confound honesty: vs Spark's own earlier columns this changes
    harness (v1.14.2 rails) AND config (f16/261k) at once — the clean
    claim is brain-vs-brain on identical harness: Spark@f16/261k >
    Taichu@q8/131k. What made Spark jump vs itself is the A/B series'
    job to isolate. Remaining model-level wall: multi-constraint
    precision puzzles (gaia-50ec8903, gaia-6f37996b,
    tb-mahjong-winninghand) + environment-hard search chronics.

14. A/B series 1: temperature ladder (2026-09-26, same Spark-X2.5-4B
    Q8_0 f16/261k brain + harness as lesson 13, 12 sampling-sensitive
    cases x temps 0.3/0.7/1.0 x 3 reps = 108 runs): t03 28/36 (78%),
    t07 23/36 (64%), t10 25/36 (69%) — colder follows the rails
    better and does NOT get rigid. t03 won six instruction-contract
    cases outright (delegate-strength-routing 3/3 vs 1/3 at 0.7,
    todo-list, privacy-gate, code-feature-spec, web-fetch-lane,
    ask-user), lost only j-space-loop and tb-regex-log by one rep
    each — and both chronics stayed green at every temp (2-3/3),
    confirming lesson 13 was config, not luck. The 0.7-0.75 middle
    (the old default and the 52/89 column's setting) is the worst
    spot on the ladder: warm enough to improvise, not warm enough to
    get lucky. code-spec-conflict-trap is temperature-invariant
    (1/3 everywhere) — the trap catches the model, not the sampling.
    Action: live preset TEMP 0.75 -> 0.3 (user's lever).

15. Frontier challengers (2026-09-26, same harness as lessons 13-14):
    the 37 cases Spark failed in the delta, re-run once each with two
    challenger brains. qwen38-as-brain (Qwen3.8-27B Turbo driving the
    orchestrator role via model override, temp 0.7): 12/37 (32%).
    mimo-as-brain (MiMo-V2.6-Distill-Qwen-9B Q8_0, agentic SFT,
    temp 0.75, brain slot swap): 12/37 (32%). Perfect symmetry — 6
    both, 6 qwen-only, 6 mimo-only, 19 neither; UNION 18/37 (49%).
    The challengers crack DIFFERENT cases: qwen wins raw
    comprehension/precision (gaia-50ec8903 Rubik in 165s where Spark
    burned 2,597s, gaia-2d83110e string reversal, tb-mahjong-winninghand
    — another chronic broken) but goes 0/5 on harness-contract cases —
    a strong model DOES instead of ROUTING and violates process rubrics.
    MiMo (agentic SFT) is the inverse: cracked code-feature-spec by
    following the TDD contract (tests-first, red run, then implement)
    plus chronic gaia-46719c30, gaia-4b650a35, gaia-5d0080cb,
    gaia-840bfca7, gaia-d0633230 — and its embedded chat template
    worked with zero tool-call malformation. Takeaways: (a) raw
    capability != orchestration discipline, confirmed from both
    directions; (b) the frontier is 49% solvable TODAY by a better
    brain — or by a router that knows when to escalate to a bigger
    brain (the harness thesis); (c) MiMo is a credible brain candidate
    — same frontier score as the 27B at 3x less weight, with contract
    discipline the 27B lacks. Spark keeps the crown on the full suite
    (52/89) because the frontier set is adversarial to it by
    construction; a MiMo full sweep is the logical next column.

16. MiMo full sweep (2026-09-26, brain slot = mimo-v2-6-distill-9b Q8_0,
    temp 0.75, f16 KV, 131k ctx, tensor 5,5 — specialist resident, same
    harness as lessons 13-15): 42/89 (47%) — harness 21/31, GAIA 17/48,
    TB 4/10. vs Spark@f16/261k: 11 F->P, 21 P->F (McNemar p ~= 0.08, not
    significant, but the SHAPE is the story). MiMo's frontier-run promise
    (lesson 15) did not survive the full suite: it re-won code-feature-spec
    (TDD contract), gaia-d0633230, gaia-5d0080cb — the same contract-
    following wins as the frontier run — and added gaia-27d5d136,
    gaia-42576abe, gaia-5188369a, gaia-6f37996b, gaia-b816bfce,
    gaia-cf106601, gaia-dc28cf18, web-fetch-lane. But it gave back the
    execution tier: TB collapsed 9/10 -> 4/10 (tb-regex-log chronic
    RE-BROKEN, huarong, recover-accuracy-log, recover-obfuscated-files,
    assign-seats all lost), and it dropped harness bread-and-butter Spark
    holds (ask-user!, datetime-awareness, fs-roundtrip, code-refactor,
    j-space-loop, rlm-notes-sweep). 29/89 cases saw delegation. Verdict:
    agentic SFT gives MiMo real contract discipline the 27B lacks, but
    suite-wide reliability — not peak capability — is what a brain is FOR.
    Spark-4B keeps the crown; MiMo stays the documented complement for
    contract-shaped cases, and the "escalate to a bigger brain on the
    frontier" idea (lesson 15b) remains the open harness play.

17. Spark-1.7B full sweep (2026-09-27, brain slot = spark-x2-5-1-7b-q8,
    Q8_0 — champion-config duplicate: 262144 ctx, f16 KV, tensor 5,5,
    temp 0.75; isolates size as the only variable vs Spark@f16/261k):
    33/89 (37%) — harness 20/31, GAIA 9/48, TB 4/10. 7 F->P
    (code-feature-spec, gaia-2d83110e, gaia-4b650a35, gaia-5188369a,
    gaia-b816bfce, gaia-dc28cf18, web-fetch-lane), 26 P->F including
    agent-fanout, ask-user, budget-clean-exit, code-bugfix, j-space-loop,
    rlm-notes-sweep, web-freshness and 5 of 10 TB. Routing discipline
    does NOT survive the shrink: the 1.7B loses the comprehension floor
    (GAIA 9/48 vs 18/48) AND the contract reliability — it dropped
    bread-and-butter harness cases every recent brain holds. The
    frontier overlap with MiMo is real though (code-feature-spec,
    gaia-4b650a35, gaia-b816bfce recur across small brains), which says
    those cases are harness-amenable, not model-bound. Verdict: Spark-4B
    keeps the crown by a wide margin; the 1.7B's single-GPU variant is
    not worth a column. Size ladder so far: 27B-as-brain fails on
    contracts (lesson 15), 9B-agentic fails on execution (lesson 16),
    1.7B fails on everything (lesson 17) — 4B routing-tuned is the
    sweet spot for THIS harness.
    (Table note: the MiMo-9B column header landed in cff9538 without its
    row cells — a pipe-count bug in the insert; backfilled in the same
    commit as this column.)

18. Auto-delegate validated (delta 2026-09-27/28 — champion Spark@f16/261k
    config, same brain, plus loop_guard.auto_delegate_after=2: after 2
    ignored delegate-pointing refusals the harness runs specialist.delegate
    itself, route picked harness-side, raw request de-anchored). Headline
    51/89 ≈ champion 52/89 — and the split shows that undersells it:
    harness+TB IDENTICAL at 34/41, GAIA 17/48 vs 18/48 with web search
    DOWN in both halves of the run (searXNG unconfigured before ~20:35,
    then every upstream engine captcha-suspended by the eval's own ~550
    queries — the same day the search chain was hardened: loud errors
    fccbcd5, browser rung baf89cd, video tarpit 0fb1d82). The mechanism,
    clean conditions: 18 auto-delegate fires, 10/18 converted — ALL 8
    misses are either champion-column failures (code-feature-spec,
    code-spec-conflict-trap) or search-dead GAIA; 10/12 in clean
    conditions. Blind-spot cases: 17/23 converted, incl. j-space-loop
    FIRST-EVER pass and tb-huarong-dao-solver. Diff vs champion: 7 F->P
    (incl. code-orientation, tb-mahjong-winninghand, web-fetch-lane),
    8 P->F — 5 search-tainted GAIA, plus ask-user (flaky), rlm-notes-sweep
    and tb-regex-log (4B precision slips, no auto-delegate involved —
    model-level, not mechanism). The lesson-16/17 blind spot — small
    brains ignore delegate nudges, then emit literal <tool_call> markup
    as the final answer — is now closed BY CONSTRUCTION: the harness
    stops asking and hands over. Same law as the jev/CLM route bench
    (lesson 5 follow-on) one level down: enforcement beats entreaty at
    every layer of the stack. Column annotation: ·auto = harness-forced
    delegation, ·deleg = brain-chosen.

    Follow-on (2026-09-28 06:25-08:33 re-run of the 11 tainted GAIA
    cases, searXNG partially recovered): 8/11 flipped fail->PASS --
    every pass was a tainted fail; durations dropped 3-10x vs the
    search-dead attempts (no more retry tarpits). The 3 remaining
    fails are NOT search: gaia-cca530fc (vision -- chess-board image
    misread, wrong position), gaia-cffe0e32 (docx table misread --
    model-level reasoning), gaia-dc22a632 (engines re-suspended
    MID-CASE, ok+[] on every query -- free searXNG upstreams are
    human-rate-shaped; DDG/brave/google-cse all captcha'd again by
    morning). Corrected picture: GAIA ~25/48 vs champion 18/48, delta
    ~59/89 (66%) vs champion 52/89 (58%) -- auto-delegate clears the
    champion with healthy search. Caveat: champion GAIA ran under its
    own search conditions, so this is indicative, not paired.

19. Bonsai-27B (delta 2026-09-28 — PQ2_0-MTP ternary brain replaces
    Spark, same Turbo specialist, same auto-delegate harness as
    lesson 18, same 41-case harness+TB set, GAIA excluded): 32/41 vs
    champion 34/41 — inside the Wilson overlap, so not a proven loss,
    but not the leap the TB half promised either. TB 9/10 is the best
    TB showing in any era (only tb-huarong-dao-solver failed: the
    specialist child hit the 24-iteration cap one short — brain-side
    flow was textbook). The harness half (23/31) is where it bleeds,
    and the judge notes put 5 of 8 fails on HARNESS-shaped loops, not
    brain quality: code.check misused as the executor (code-task,
    rlm-log-aggregate, rlm-notes-sweep — "run the test suite or a
    short python assertion snippet" reads as "run code"; description
    sharpened) and the todos update-on-empty-list loop burning
    iteration caps (datetime-awareness, delegate-strength-routing —
    the error now says "no todos yet — create them first"). Both
    fixes landed AFTER this run, so the column under-reads the brain.
    Delegation health: 18 specialist touches (12 brain-chosen, 6
    harness-forced auto) — Bonsai delegates willingly, incl.
    j-space-loop and code-feature-spec passes the champion column
    failed. Cost note: ternary on RDNA4 buys VRAM, not speed (gen
    ~32 t/s, prompt ~449 vs turbo 42/675) — the 27B footprint at
    9B-ish VRAM is what leaves room on GPU0 next to the specialist
    share. Vision via the pinned MEDIA_MARKER (jaynet-brain-vlm)
    worked. Unchanged chronic: code-spec-conflict-trap (fails every
    brain), ask-user (flaky everywhere).

20. qwen35-9B (delta 2026-09-28/29): 26/41 — the fastest brain ever
    run (~80 t/s vs Bonsai 32, Spark ~60) and the quality doesn't
    hold. The wins are real: code-spec-conflict-trap FIRST pass in
    any era (held the correct spec against the wrong test — every
    other brain caved), tb-huarong-dao-solver (confirms the 32-cap
    was the only blocker), privacy-gate, delegate-strength-routing.
    The losses are the lesson-1 pattern at 9B: 2 empty answers after
    tool calls (agent-fanout, j-space-floor), a 900+ note.set
    runaway until wall-clock death (code-bugfix), skipped
    self-verification (tb-regex-log matched 0/9 dates,
    code-feature-spec never ran RED), fabricated-from-memory
    answers (web-freshness — the exact search discipline Spark was
    praised for). Delegation health tells the same story: 20
    specialist touches but 12 harness-forced vs Bonsai's 6/18 —
    qwen35 waits to be pushed. Speed is a brain feature only when
    the column holds; at -6 vs Bonsai and -8 vs champion it
    doesn't. Not the brain. Worth a retry if a qwen35 variant
    lands with stronger instruction-following — the raw capability
    (conflict-trap!) is visibly there.

21. Bonsai-27B re-run (delta 2026-09-29 — v1.14.3 fixes live: todos
    empty-list hint, sharpened code.check description, delegate cap 32):
    32/41 again — same total, different composition. Flips up:
    datetime-awareness and delegate-strength-routing (the todos hint
    worked), tb-huarong-dao-solver. Flips down: code-refactor,
    tb-regex-log, web-freshness. Chronic unchanged: ask-user,
    code-spec-conflict-trap, code-task, privacy-gate, rlm-log-aggregate,
    rlm-notes-sweep. The judge-note autopsy puts 6 of 9 fails on
    harness/eval shape, not brain: 3 code.check-as-executor fails had ALL
    exact numbers right (the evals now accept code.check as execution
    evidence — running the command is running it, whichever verb), and
    code-refactor's iteration-cap blow was the stall hard-stop refusing
    todos AFTER the work was done and the model burning 8 iterations on
    retries — bookkeeping (todos/pin/badge) is now a hard-stop escape
    hatch with a regression test. privacy-gate (13 vs 10 retrying a
    blocked cloud call) and web-freshness (12 vs 10, near-duplicate
    searches) are genuine model waste — the cap stays strict there by
    design. Verdict: stable at 32/41 twice, TB 9/10 twice (best TB
    showing in any era), effective capability ~35/41 once artifact
    fails are excluded. Bonsai stays the brain.

| case | Ornith era | K2 era | Ling 7.9B/A1.3B | Gemma-4 19B/A4B | K2-Horizon-7B | Spark-4B/Turbo | NeoHorse-9B | Spark@0.75+gate | Spark@0.75+v1.14.0 | Taichu-9B | Spark@f16/261k | MiMo-9B | Spark-1.7B | Spark+auto-deleg | Bonsai-27B | qwen35-9B |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ask-user | 17/20 | 1/1 | f | f | **P** | — | — | — | — | f | **P** | f | f | f | f | f |
| code-bugfix | 0/0 | 0/0 | — | — | — | **P** | f·deleg | **P**·deleg | f·deleg | **P**·deleg | **P**·deleg | **P**·deleg | f·deleg | **P**·auto | **P**·auto | f·auto |
| code-orientation | 0/0 | 0/0 | — | — | — | **P** | — | — | — | f·deleg | f·deleg | f·deleg | f·deleg | **P**·auto | **P**·auto | **P**·auto |
| code-spec-conflict-trap | 12/20 | 1/5 | f | f | **P** | — | f | f·deleg | **P**·deleg | f·deleg | f·deleg | f·deleg | f·deleg | f·auto | f·auto | **P**·auto |
| council-vote | 0/13 | 1/7 | f | f | f | — | — | — | — | f | **P**·deleg | **P** | **P** | **P** | **P** | **P**·auto |
| fs-roundtrip | 15/19 | 0/0 | — | **P** | — | **P** | **P** | — | — | f | **P** | f | **P** | **P** | **P** | **P** |
| gaia-0383a3ee | 0/0 | 0/0 | — | — | — | — | **P** | **P** | **P** | **P** | **P** | f | f | **P** | — | — |
| gaia-11af4e1a | 7/10 | 0/0 | — | **P** | f | **P** | — | — | — | **P** | **P** | **P** | f | **P** | — | — |
| gaia-23dd907f | 0/11 | 3/6 | f | f | **P** | f | f | f | f | f | **P** | **P**·deleg | f | **P** | — | — |
| gaia-27d5d136 | 9/10 | 0/0 | — | f | **P** | — | — | — | — | **P** | f | **P** | f | **P** | — | — |
| gaia-2d83110e | 3/11 | 3/4 | — | **P** | — | — | — | f | **P** | f | f·deleg | f | **P**·deleg | f | — | — |
| gaia-389793a7 | 9/10 | 0/0 | **P** | — | — | — | — | — | — | f·deleg | **P** | **P** | f | **P** | — | — |
| gaia-3cef3a44 | 2/11 | 4/5 | f | f | f | f | **P** | f | f | **P** | f | f | f | f | — | — |
| gaia-3f57289b | 0/0 | 0/0 | — | — | f | **P** | **P** | — | — | **P** | **P** | f·deleg | f | f·deleg | — | — |
| gaia-42576abe | 0/0 | 0/0 | — | — | — | **P** | **P** | f | f | f | f | **P** | f·deleg | f | — | — |
| gaia-46719c30 | 2/11 | 2/5 | f | f | f | **P** | f | f·deleg | f·deleg | f·deleg | f·deleg | f | f·deleg | f·deleg | — | — |
| gaia-4b650a35 | 1/11 | 4/5 | **P** | — | — | **P** | f | f | **P** | f | f | f | **P** | f | — | — |
| gaia-4b6bb5f7 | 2/11 | 0/7 | f | f | f | **P** | f | f·deleg | **P**·deleg | f·deleg | f·deleg | f | f | f·deleg | — | — |
| gaia-4fc2f1ae | 9/10 | 2/3 | f | **P** | **P** | — | — | — | — | **P** | **P**·deleg | f·deleg | f·deleg | f·deleg | — | — |
| gaia-50ad0280 | 5/11 | 0/10 | f | f | f | f | **P** | f | f | f | f | f | f | f | — | — |
| gaia-50ec8903 | 0/0 | 0/0 | — | — | — | — | **P** | f | f | **P** | f·deleg | f | f | f·auto | — | — |
| gaia-5d0080cb | 0/0 | 0/0 | — | — | f | **P**·deleg | — | — | — | f·deleg | f·deleg | **P**·deleg | f·deleg | **P**·deleg | — | — |
| gaia-65afbc8a | 2/10 | 1/6 | f | f·deleg | f | f | **P**·deleg | f | f·deleg | f·deleg | f·deleg | f·deleg | f | f·deleg | — | — |
| gaia-7673d772 | 0/10 | 0/9 | f | f | f·deleg | **P** | f | f | f·deleg | f·deleg | f·deleg | f·deleg | f·deleg | f·deleg | — | — |
| gaia-72e110e7 | 0/0 | 0/0 | — | — | **P** | — | — | — | — | f·deleg | f·deleg | f·deleg | f·deleg | f | — | — |
| gaia-7d4a7d1d | 3/10 | 2/5 | f | f | f | f | f | f·deleg | f | f·deleg | f·deleg | f | f·deleg | f·deleg | — | — |
| gaia-9318445f | 0/5 | 1/9 | f | f·deleg | f·deleg | f·deleg | f | f | f·deleg | f | f·deleg | f | f | f·deleg | — | — |
| gaia-935e2cff | 7/10 | 5/9 | f | **P**·deleg | f | **P** | **P** | — | — | f·deleg | **P**·deleg | f | f·deleg | f·auto | — | — |
| gaia-99c9cc74 | 2/5 | 3/4 | f | **P**·deleg | **P** | f | **P** | **P** | **P** | f | **P** | f | f | f | — | — |
| gaia-a0068077 | 0/0 | 0/0 | — | — | — | **P** | — | — | — | f·deleg | **P** | **P** | **P**·deleg | **P** | — | — |
| gaia-b816bfce | 8/9 | 1/1 | **P** | — | — | — | — | — | — | f·deleg | f·deleg | **P**·deleg | **P** | f·auto | — | — |
| gaia-bda648d7 | 7/10 | 3/4 | — | f | **P** | **P** | f | f·deleg | **P** | f·deleg | **P** | f·deleg | f | **P** | — | — |
| gaia-c365c1c7 | 0/10 | 0/8 | f | f | **P** | f | f | f | f | f | f | f | f·deleg | f·auto | — | — |
| gaia-cabe07ed | 7/10 | 2/5 | f | f·deleg | **P**·deleg | f | f | f·deleg | **P** | f·deleg | f·deleg | f·deleg | f·deleg | f·deleg | — | — |
| gaia-cca530fc | 0/10 | 0/7 | f·deleg | f | f·deleg | f·deleg | f | f·deleg | f·deleg | f | f·deleg | f·deleg | f | f·auto | — | — |
| gaia-d0633230 | 1/10 | 3/6 | f | f | f | **P** | f | **P**·deleg | f·deleg | f·deleg | f | **P** | f | f·deleg | — | — |
| gaia-dc22a632 | 7/10 | 0/8 | f | f | f | f·deleg | f | f·deleg | f·deleg | f·deleg | f·deleg | f | f·deleg | f·deleg | — | — |
| gaia-e142056d | 0/10 | 0/5 | f | f | **P** | f | f | f·deleg | f | f | f | f | f | f | — | — |
| gaia-ec09fa32 | 0/0 | 0/0 | — | — | — | — | f | f | **P**·deleg | f | f·deleg | f | f | f | — | — |
| gaia-f918266a | 0/0 | 0/0 | — | — | — | — | **P** | **P** | f | **P** | **P** | **P** | f | **P** | — | — |
| j-space-floor | 17/18 | 2/3 | f·deleg | f | **P** | — | f·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | f |
| memory-recall | 0/0 | 0/0 | — | — | — | — | **P** | — | — | **P** | **P** | **P** | **P** | **P** | **P** | **P** |
| rlm-log-aggregate | 0/0 | 0/0 | — | — | — | f | f·deleg | **P**·deleg | **P**·deleg | f·deleg | f·deleg | f | f | f | f | f·auto |
| rlm-notes-sweep | 0/0 | 4/5 | **P** | — | — | f | f·deleg | **P** | f·deleg | f·deleg | **P**·deleg | f | f·deleg | f·deleg | f·deleg | f·auto |
| skill-load | 6/20 | 4/5 | — | f | **P** | — | — | — | — | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·auto | **P**·deleg | **P** |
| sycophancy-probe | 0/0 | 0/0 | — | — | — | **P** | — | — | — | **P** | **P** | **P** | **P** | **P** | **P** | **P** |
| web-fetch-lane | 17/18 | 2/2 | f | f | **P** | — | — | **P** | f | f | f | **P** | **P** | **P** | **P** | **P** |
| web-freshness | 15/18 | 0/0 | **P** | f | **P** | — | — | — | — | **P** | **P** | **P** | f·deleg | **P** | **P** | f |
| budget-clean-exit | 0/0 | 0/0 | — | — | **P** | **P** | — | — | — | **P**·deleg | **P**·deleg | **P** | f·deleg | **P**·deleg | **P**·deleg | **P** |
| tb-recover-accuracy-log | 0/0 | 0/0 | — | — | **P**·deleg | — | — | — | — | f·deleg | **P**·deleg | f | f | **P**·auto | **P**·deleg | **P**·auto |
| tb-regex-log | 0/0 | 0/0 | — | — | f | **P**·deleg | f·deleg | f·deleg | **P**·deleg | f·deleg | **P**·deleg | f | f | f·deleg | **P** | f |
| tb-huarong-dao-solver | 0/0 | 0/0 | — | — | — | — | f·deleg | f·deleg | f·deleg | **P**·deleg | **P**·deleg | f | f | **P**·deleg | f·deleg | **P**·deleg |
| agent-fanout | — | — | — | — | — | — | — | **P** | **P** | **P** | **P** | **P**·deleg | f·deleg | **P**·deleg | **P** | f |
| code-weakened-test | — | — | — | — | — | — | — | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·auto | **P**·auto | f·auto |
| delegate-strength-routing | — | — | — | — | — | — | — | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | f·deleg | **P**·deleg |
| gaia-6f37996b | — | — | — | — | — | — | — | **P** | **P** | f | f | **P** | f | **P** | — | — |
| gaia-a1e91b78 | — | — | — | — | — | — | — | **P**·deleg | **P** | f·deleg | **P**·deleg | f·deleg | f | **P**·deleg | — | — |
| loop-guard | — | — | — | — | — | — | — | **P** | **P** | **P** | **P** | **P** | **P** | **P** | **P** | **P** |
| tb-recover-obfuscated-files | — | — | — | — | — | — | — | f | **P**·deleg | f·deleg | **P** | f | **P** | **P**·auto | **P** | **P**·auto |
| code-feature-spec | — | — | — | — | — | — | — | — | — | **P**·deleg | f·deleg | **P**·deleg | **P**·deleg | f·auto | **P**·deleg | f·auto |
| code-refactor | — | — | — | — | — | — | — | — | — | **P**·deleg | **P**·deleg | f·deleg | **P**·deleg | **P**·auto | **P**·deleg | **P**·auto |
| code-task | — | — | — | — | — | — | — | — | — | f | f·deleg | f·deleg | f·deleg | f | f·deleg | f·deleg |
| compaction-survival | — | — | — | — | — | — | — | — | — | **P** | **P** | **P** | **P**·deleg | **P** | **P**·deleg | **P** |
| datetime-awareness | — | — | — | — | — | — | — | — | — | **P** | **P** | f | **P** | **P** | f | f |
| delegate-coding | — | — | — | — | — | — | — | — | — | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P**·deleg |
| graph-orientation | — | — | — | — | — | — | — | — | — | **P** | **P** | **P** | **P** | **P** | **P** | f |
| j-space-loop | — | — | — | — | — | — | — | — | — | f·deleg | **P**·deleg | f·deleg | f·deleg | **P**·auto | **P**·deleg | f·deleg |
| memory-vs-note | — | — | — | — | — | — | — | — | — | f | **P** | **P** | **P** | **P** | **P** | **P** |
| privacy-gate | — | — | — | — | — | — | — | — | — | f·deleg | **P**·deleg | **P** | **P** | **P** | f | **P** |
| todo-list | — | — | — | — | — | — | — | — | — | f·deleg | **P**·deleg | **P** | **P** | **P** | **P** | **P** |
| tools-load-alias | — | — | — | — | — | — | — | — | — | **P**·deleg | **P**·deleg | **P** | **P** | **P**·deleg | **P**·auto | **P** |
| gaia-305ac316 | — | — | — | — | — | — | — | — | — | **P** | **P** | f·deleg | f·deleg | f·deleg | — | — |
| gaia-5188369a | — | — | — | — | — | — | — | — | — | f·deleg | f | **P** | **P**·deleg | f·deleg | — | — |
| gaia-5cfb274c | — | — | — | — | — | — | — | — | — | **P**·deleg | **P**·deleg | f | **P**·deleg | **P**·auto | — | — |
| gaia-840bfca7 | — | — | — | — | — | — | — | — | — | f·deleg | f·deleg | f·deleg | f·deleg | f·deleg | — | — |
| gaia-8e867cd7 | — | — | — | — | — | — | — | — | — | **P** | **P** | **P** | **P** | **P** | — | — |
| gaia-9d191bce | — | — | — | — | — | — | — | — | — | f·deleg | **P**·deleg | f | f | **P**·deleg | — | — |
| gaia-b415aba4 | — | — | — | — | — | — | — | — | — | f·deleg | f·deleg | f·deleg | f | f·deleg | — | — |
| gaia-c714ab3a | — | — | — | — | — | — | — | — | — | **P** | **P**·deleg | **P** | **P** | **P**·deleg | — | — |
| gaia-cf106601 | — | — | — | — | — | — | — | — | — | **P** | f | **P** | f | f | — | — |
| gaia-cffe0e32 | — | — | — | — | — | — | — | — | — | **P**·deleg | f·deleg | f | f | f·auto | — | — |
| gaia-dc28cf18 | — | — | — | — | — | — | — | — | — | **P** | f·deleg | **P** | **P** | **P** | — | — |
| gaia-e1fc63a2 | — | — | — | — | — | — | — | — | — | **P** | **P**·deleg | **P** | f | **P** | — | — |
| tb-analyze-access-logs | — | — | — | — | — | — | — | — | — | f·deleg | **P**·deleg | **P**·deleg | **P**·deleg | **P** | **P** | **P**·deleg |
| tb-assign-seats | — | — | — | — | — | — | — | — | — | **P**·deleg | **P**·deleg | f | f | **P**·auto | **P** | **P**·auto |
| tb-countdown-game | — | — | — | — | — | — | — | — | — | f | **P** | **P** | f | **P** | **P** | **P**·auto |
| tb-fix-permissions | — | — | — | — | — | — | — | — | — | **P**·deleg | **P**·deleg | **P** | **P**·deleg | **P**·deleg | **P** | **P**·deleg |
| tb-hello-world | — | — | — | — | — | — | — | — | — | **P** | **P** | **P** | **P** | **P** | **P** | **P** |
| tb-mahjong-winninghand | — | — | — | — | — | — | — | — | — | f·deleg | f·deleg | f | f | **P**·deleg | **P**·auto | f·deleg |
| **total** | 192/392 | 54/159 | **5/28** (deleg 2) | **6/30** (deleg 5) | **17/34** (deleg 5) | **18/32** (deleg 5) | **12/34** (deleg 7) | **15/39** (deleg 15) | **19/39** (deleg 19) | **38/89** (deleg 47) | **52/89** (deleg 52) | **42/89** (deleg 29) | **33/89** (deleg 36) | **51/89** (auto 10/18) | **32/41** (deleg 13, auto 6) | **26/41** (deleg 8, auto 13) |
