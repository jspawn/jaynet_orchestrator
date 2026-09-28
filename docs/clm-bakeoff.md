# Route bench — decision models vs keyword router

- date: 2026-09-27 18:49, jevify columns 2026-09-28
- scorers: kw, clm, jev, jevify (Qwen3.8-27B-Turbo + Ternary-Bonsai-2-27B) · threshold 0.6
- set: 243 requests — shipped harness cases, imported gaia/tb, 16 hand-labeled chat states
- ground truth by case family (labels noisy for tb; noise is symmetric)
- keyword side = routing_nudge.strength_keywords as shipped (security-only; a miss routes nothing → 'general')

## Headline

| scorer | top-1 accuracy |
|---|---|
| keyword router (today) | 32/243 = **13.2%** |
| CLM raw | 46/243 = **18.9%** |
| hosted Jev raw | 178/243 = **73.3%** |
| CLM @ threshold 0.6 (as the hook ships) | 37/243 = **15.2%** |
| hosted Jev @ threshold 0.6 (as the hook ships) | 171/243 = **70.4%** |
| jevify on Qwen3.8-27B-Turbo (local, specialist slot) | 155/243 = **63.8%** |
| jevify on Ternary-Bonsai-2-27B (local, specialist slot) | 145/243 = **59.7%** |
| jevify/Turbo @ threshold 0.6 | 139/243 = **57.2%** |
| jevify/Bonsai @ threshold 0.6 | 124/243 = **51.0%** |

## Per-class accuracy

| ground truth | n | kw | clm | jev | jevify-turbo | jevify-bonsai | clm@thr | jev@thr |
|---|---|---|---|---|---|---|---|---|
| coding | 144 | 0% | 22% | 83% | 72% | 64% | 10% | 77% |
| creative | 2 | 0% | 0% | 100% | 100% | 100% | 0% | 100% |
| general | 24 | 92% | 17% | 62% | 50% | 75% | 58% | 75% |
| multi-step | 6 | 0% | 0% | 17% | 67% | 33% | 0% | 17% |
| reasoning | 2 | 0% | 100% | 100% | 100% | 100% | 100% | 100% |
| research | 54 | 0% | 0% | 54% | 39% | 39% | 0% | 52% |
| security | 11 | 91% | 82% | 82% | 91% | 73% | 64% | 82% |

## clm latency

- p50 0.00s · p95 0.00s · max 0.01s
- over the hook's 2.0 s route_timeout_s: 0.0% (those defer to keywords live)

## jev latency

- p50 0.28s · p95 0.34s · max 0.42s
- over the hook's 2.0 s route_timeout_s: 0.0% (those defer to keywords live)

## jevify latency (local sidecar, specialist slot)

- Qwen3.8-27B-Turbo: p50 1.29s · p95 2.23s · max 2.52s — 11.5% over the hook's
  2.0 s route_timeout_s (those defer to keywords live)
- Ternary-Bonsai-2-27B: p50 0.87s · p95 1.67s · max 2.00s — 0.0% over budget

## Follow-on (2026-09-28): jevify closes the local gap

The 2026-09-27 verdict ("CLM too weak, Jev is cloud, hooks stay off") had a
hole: no LOCAL scorer was anywhere near hosted Jev. jevify — the open-jev
sidecar driving the specialist slot through the same /v1/systemone contract —
fills it: 63.8% top-1 on Qwen3.8-27B-Turbo, 59.7% on Ternary-Bonsai-2-27B,
both vs Jev's 73.3% and CLM's 18.9%. Latency is the trade: Jev answers in
0.3s flat from the cloud; the local 27Bs need ~1s warm (Turbo p95 2.23s
breaches the 2s hook budget on 11.5% of calls, Bonsai fits it). Routing
quality still isn't hook-grade — research sits at 39% for every local scorer
— so hooks stay OFF, but jevify-on-specialist is now the default measuring
stick and the first local option that beats "keywords + delegate nudges" by a
margin worth having. Bonsai note: benched single-GPU (GPU1) — the prism
fork's meta backend aborts in ggml_backend_meta_get_split_state on any
--split-mode tensor (gfx1201); single-GPU ROCm and CPU are fine. Bonsai is
text-only (no mmproj), so it does not bring vision to the brain slot.

## Sample clm misroutes (first 20)

| case | truth | said | p |
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

## Sample jev misroutes (first 20)

| case | truth | said | p |
|---|---|---|---|
| budget-clean-exit | general | research | 0.79 |
| graph-orientation | general | coding | 1.00 |
| j-space-floor | general | coding | 0.99 |
| j-space-loop | general | coding | 1.00 |
| privacy-gate | general | coding | 0.54 |
| rlm-notes-sweep | research | coding | 0.78 |
| skill-load | general | coding | 1.00 |
| todo-list | general | multi-step | 0.50 |
| gaia-11af4e1a | research | general | 0.46 |
| gaia-27d5d136 | research | reasoning | 1.00 |
| gaia-2d83110e | research | general | 0.80 |
| gaia-389793a7 | research | reasoning | 0.92 |
| gaia-3cef3a44 | research | reasoning | 0.67 |
| gaia-42576abe | research | reasoning | 0.65 |
| gaia-4b650a35 | research | reasoning | 0.48 |
| gaia-50ad0280 | research | general | 0.36 |
| gaia-50ec8903 | research | reasoning | 0.71 |
| gaia-5cfb274c | research | reasoning | 0.70 |
| gaia-65afbc8a | research | multi-step | 0.53 |
| gaia-6f37996b | research | reasoning | 0.98 |

## Latency caveat

The clm latencies above are **cache-warm**: clm-serve had embedded these
exact states during the first (cold) run at 17:43 and served them from its
internal pool. Cold-cache numbers from that run: p50 2.6 s / p95 9.8 s,
65.8% over the hook's 2.0 s budget. Live traffic is unique prompts, so the
cold numbers are the honest ones. Jev has no local cache — 0.3 s flat.

## Verdict (2026-09-27, three-way)

**The idea holds at scale; the local implementations don't.**

- **Hosted Jev: 73.3% top-1** (70.4% @ shipping threshold), ~0.3 s flat —
  confirms lesson 5 (brain-bakeoff) on 243 cases instead of 20: a decision
  model trained for routing routes. It is the only scorer that claims
  coding (83%) and research (54%) at rates that would change real routing.
  Its residual "errors" are partly label noise (GAIA marked research often
  IS a reasoning question; jev says reasoning at p=0.9+).
- **CLM v0.1: 18.9%** — same failure shape as Open-Jev 2B before it:
  a reasoning prior it can't shake (research 0%, stripped GAIA still
  "reasoning" p=0.96), calibration detached from correctness, cold
  latency 8× over the hook budget on CPU. Not routing-trained enough.
- **Keyword router: 13.2%** — narrow by design: only claims security,
  claims it well (91%), never misroutes general traffic into specialists
  (92% general). It stays the default.

Consequences:

- `plugins.clm.route` stays **false**; `clm.decide`/`clm.rank` remain as
  tools (council scoring, best-of-N) where latency is irrelevant.
- `plugins.jev.route` stays **false** too — not because it doesn't work
  (it does, decisively) but because every request's text would leave the
  box. That is the standing local-first verdict from lesson 5, unchanged.
- The fourth column — **jevify** (local specialist as the decision backend)
  — is the open question that matters: if a local 27B with the jevify
  recipe lands anywhere near Jev's 73%, local learned routing becomes real.
  Queued for when the specialist slot is free (post-delta).
- Routing mechanism unchanged: keywords + loop-guard auto-delegate.
