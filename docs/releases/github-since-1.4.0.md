# JayNet v1.14.3 — five weeks, 22 tags: the measured-orchestrator release line

**Since v1.4.0 (2026-08-26):** the orchestrator grew from "a good agent loop"
into a measured, self-hardening multi-model system — a thirteen-candidate
brain bakeoff, an enforcement pipeline that replaced prompt-begging with
mechanisms, a six-plugin capability layer, multi-GPU model lifecycle, and a
closed eval loop that turns real failures into fixes. 2000+ tests green,
five external audits closed along the way.

## The brain search (the headline)

The orchestrator brain is the harness's multiplier, so we stopped picking it
by vibes. Thirteen candidates — 35B MoEs down to a 1.7B, dense / MoE /
ternary — ran the same hard-tail eval suite through the *real* agent loop;
every column is comparable in
[docs/brain-bakeoff.md](docs/brain-bakeoff.md). What the table taught:

- **A small routing-tuned brain + a strong specialist beats a big brain that
  hogs the wheel.** Champion config: Spark-X2.5-4B (4B MoE) + a tensor-split
  27B coding specialist — 52/89 on the hard tail.
- **Delegation count is a better brain-health metric than pass rate** — a
  brain that won't route makes the specialist architecture decorative.
- **Speed is a feature only while the pass column holds:** the 80 t/s
  candidate (qwen35-9B) lost 26 vs 32 to the 32 t/s Ternary-Bonsai-2-27B,
  the current driver — 27B mass at ~9 GB VRAM with working vision.
- **The "models won't delegate" problem is not solved by prompting —
  anywhere.** The research sweep found the same failure across frameworks;
  the answer is enforcement (below).

## Enforcement over prompting — the guard pipeline (1.7 → 1.14)

Every rung of this ladder was measured on evals before it shipped: routing
nudge → strength gate (inline edits rejected until delegated) → stall ladder
(count *product*, not activity) → stall hard-stop → bounce cap → deliverable
check → exactness gate → **auto-delegate** (the loop guard runs the
delegation itself after ignored rejections — 10/12 blind-spot conversions)
→ **delegation review** (a fresh-context judgment of the specialist's report
by the strongest available model — never the brain). Plus **procedures**:
shape-tagged playbooks distilled from frontier-model process, auto-loaded on
a confident match, their checkpoints nudged against before an answer is
accepted. And brain tool gating: with a coding specialist present, the
brain's coding tools become verify-only (`brain_mode: verify` /
`dispatch`).

## Models as infrastructure (1.9)

Any model claims any subset of the machine's GPUs — swap planner, tensor
splits, per-slot ctx/KV — and delegate swaps put everything back when the
task is done. Presets export/import as shareable `.jaypack` files, a
duplicate button drafts A/B variants in one click, HuggingFace downloads run
from the admin UI, and already-running servers (vLLM, Ollama, another
llama.cpp box) adopt as remote presets. CPU helper slots cover embeddings,
rerank, whisper STT (the mic button) and vision.

## Plugins: from zero to six (1.11 → 1.14)

Toggleable capability bundles with live enable/disable: **graphify**
(per-project code/doc graphs), **benchlab** (Terminal-Bench + GAIA imports),
**h5i** (policy-controlled Rust browser lane), **jev** + **clm** (decision
models — measured, documented, ships off: keywords win for now),
**imagegen** (local text-to-image: hibernates the specialist for the VRAM,
draws, hands you the PNG, swaps back). Plus **connectors** — declarative,
shareable bridges to external systems (read-only/read-write per connector).

## The eval loop closes (1.5 → 1.14)

Real failure → one-click regression case → judge proposal → apply to the
custom layer → re-measure. The suite runs Terminal-Bench and GAIA through
the real loop with their own graders; benchmark variants compare brains,
samplers and skills head-to-head; the judge itself is calibrated against
frozen transcripts. Every bakeoff column above ran on this.

## Hardening (audits #10 – #24)

Privacy taint-tracking through every new path (review calls, routing hooks,
subcalls), scratch isolation per run, scheduled-run governance, CI with
ruff, a mypy baseline gate (host-independent), pip-audit on all lockfiles
and a catalog freshness gate. Docs stay honest: README, playbook, learning
guide (§3.16 tells the brain-search story) and 28 screenshots track the
product.

---

*Full detail: [CHANGELOG.md](CHANGELOG.md) and per-version notes in
[docs/releases/](docs/releases/). Upgrade: `git pull`, restart the two
services — see docs/upgrading.md.*
