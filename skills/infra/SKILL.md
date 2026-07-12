# Infrastructure & Operations

**Trigger:** serve, model, ops, job, status, eval, council, compare, debate

## Model serving
* `serve.start` — launch a local model server (pinned to GPU 1 by default). Registers as a LiteLLM alias for `llm.call`/`agent.spawn`.
* `serve.stop` / `serve.list` / `serve.status` / `serve.health` — manage served models.
* `model.list` — show preset catalog with GPU/port, live status, free VRAM.
* `model.use` — ensure a preset is served and return the alias. Default posture: brain on GPU 0 (:8090), coder/brain2 on GPU 1 (:8080). Loading a 35B model costs tens of seconds — prefer live models.

**Note:** `serve.*` only tracks models YOU launched with `serve.start`. For systemd-managed units (LiteLLM proxy, brains), use `ops.status`.

## Operations
* `ops.status` — one-call stack health: checks systemd services + endpoint pings. **Run this FIRST before debugging live services.**
* `ops.run` — single allowlisted command on the host (pytest, curl, systemctl, rocm-smi). Confirmation-gated. For trusted self-validation of the LIVE box.

## Jobs
* `job.start` — launch a long-running, detached, GPU-capable command. Returns `job_id` immediately.
* `job.status` / `job.logs` — monitor. `job.wait` — block with timeout (use instead of hand-rolling a poll loop).
* `job.list` / `job.cancel` — manage active jobs.
* Check `gpu.status` before launching anything heavy.

## Evaluation & deliberation
* `eval.compare` — run one prompt across several models to compare output/cost. Spends real money deliberately.
* `council.debate` — multi-model deliberation for genuinely contested decisions. Brain + coder (+ optional cloud) reason, rebut, and you synthesize. Use `panel` with `{model, persona}` for viewpoints. Reserve for questions where independent perspectives help — not factual lookups.
