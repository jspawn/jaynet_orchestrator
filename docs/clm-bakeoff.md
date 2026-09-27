# CLM route bench — System One vs keyword router

- date: 2026-09-27 17:43
- clm: http://127.0.0.1:8700 (encoder Qwen3-8B Q8_0, CPU), threshold 0.6
- set: 243 requests — shipped harness cases, imported gaia/tb, 16 hand-labeled chat states
- ground truth by case family (labels noisy for tb; noise is symmetric)
- keyword side = routing_nudge.strength_keywords as shipped (security-only; a miss routes nothing → 'general')

## Headline

| scorer | top-1 accuracy |
|---|---|
| keyword router (today) | 32/243 = **13.2%** |
| CLM raw (no threshold) | 46/243 = **18.9%** |
| CLM @ threshold 0.6 (as the hook ships) | 37/243 = **15.2%** |

CLM↔keyword agreement: 20.6%

## Per-class accuracy (n, keyword, CLM raw, CLM@thr)

| ground truth | n | keyword | CLM raw | CLM@thr |
|---|---|---|---|---|
| coding | 144 | 0% | 22% | 10% |
| creative | 2 | 0% | 0% | 0% |
| general | 24 | 92% | 17% | 58% |
| multi-step | 6 | 0% | 0% | 0% |
| reasoning | 2 | 0% | 100% | 100% |
| research | 54 | 0% | 0% | 0% |
| security | 11 | 91% | 82% | 64% |

## CLM latency (CPU encoder)

- p50 2.60s · p95 9.84s · max 12.33s
- over the hook's 2.0 s route_timeout_s: 65.8% of calls (those defer to keywords live)

## Sample CLM misroutes (first 20)

| case | truth | CLM said | p |
|---|---|---|---|
| agent-fanout | general | reasoning | 0.96 |
| ask-user | general | coding | 0.38 |
| budget-clean-exit | general | reasoning | 0.54 |
| code-task | coding | reasoning | 0.70 |
| compaction-survival | general | reasoning | 0.86 |
| council-vote | general | reasoning | 0.95 |
| datetime-awareness | general | reasoning | 0.68 |
| graph-orientation | general | coding | 0.50 |
| injection-attachment | general | reasoning | 0.50 |
| injection-web-page | general | reasoning | 0.49 |
| j-space-floor | general | coding | 0.51 |
| j-space-loop | general | coding | 0.81 |
| memory-recall | general | vision | 0.43 |
| rlm-log-aggregate | coding | general | 0.43 |
| rlm-notes-sweep | research | reasoning | 0.76 |
| skill-load | general | coding | 0.65 |
| sycophancy-probe | general | reasoning | 0.78 |
| todo-list | general | reasoning | 0.46 |
| tools-load-alias | general | coding | 0.38 |
| web-fetch-lane | research | security | 0.75 |

## Verdict (2026-09-27)

**Do not enable the route hook.** Three independent reasons, each sufficient:

1. **Accuracy:** CLM 18.9% raw vs keyword router 13.2% — not a meaningful
   win, and the shipping threshold config drops it to 15.2% (defers land on
   the same keyword baseline). Per class: research 0% (a stripped GAIA
   question still routes "reasoning" at p=0.96 — the boilerplate is NOT the
   cause), coding 22%, multi-step 0%. CLM v0.1 has a strong *reasoning*
   prior over this 7-tag taxonomy.
2. **Latency:** p50 2.6 s / p95 9.8 s on the CPU encoder — 65.8% of real
   requests blow the hook's 2.0 s route_timeout_s and would defer to
   keywords anyway. A GPU encoder (Q4_K_M ~5 GB) might fix latency but not
   accuracy.
3. **Calibration:** confidence is detached from correctness (misroutes at
   p=0.96), so thresholding cannot rescue it.

Consequences:

- `plugins.clm.route` stays **false**; the route_request hook idea is parked
  (same verdict class as jev/jevify before it — see docs/brain-bakeoff.md,
  lesson on delegation classifiers).
- The jevify side-by-side is moot: CLM did not clear the bar jevify would
  have had to beat.
- `clm.decide` / `clm.rank` remain available as *tools* (council-style
  scoring, pairwise ranking) where latency is irrelevant — that use was
  never the problem.
- The harness keyword router + auto-delegate (loop_guard) stays the routing
  mechanism. Its 13.2% top-1 here understates it: it only claims security,
  and it claims that well (91%).
