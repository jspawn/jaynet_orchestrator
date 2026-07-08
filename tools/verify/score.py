"""verify.* — LLM-as-a-verifier with continuous scores (logit-expectation).

Instead of asking a judge for a discrete score and taking the emitted token, we
read the logprobs over an ordered set of single-token grades and take the
EXPECTATION — a continuous, tie-free score. Scales along three axes from the
"LLM-as-a-Verifier" paper: granularity G (number of grade levels), repeats K
(averaged to cut variance), and criteria C (decompose the judgement, average).

The verifier model is a LiteLLM alias, resolved in this order so it's easy to
retarget as better/smaller verifiers appear:
    verify.model (runtime.yaml)  ->  ORCH_VERIFIER_MODEL (.env)  ->  the brain.

Needs a backend that returns token logprobs — llama.cpp (n_probs) and LiteLLM's
OpenAI-compatible top_logprobs both do.

  verify.score(solution, task=…, criteria=[…])   -> continuous score in [0,1] + breakdown
  verify.rank(candidates=[…], task=…)             -> best-of-N: ranked list + best index
"""

from __future__ import annotations

import math
import os
import string

import httpx

from runtime.tool_base import Tool, ToolContext, ToolResult

_DEFAULT_CRITERIA = [
    "Correctness: factually and logically correct for the task.",
    "Completeness: fully addresses everything the task asks for.",
    "Quality: clear, well-structured, and free of errors.",
]


def _vcfg(ctx: ToolContext) -> dict:
    return ctx.config.get("verify", {}) or {}


def _verifier_model(ctx: ToolContext, override: str | None = None) -> str:
    """Resolve the verifier alias: explicit arg > config > env > the brain."""
    return (override
            or _vcfg(ctx).get("model")
            or os.environ.get("ORCH_VERIFIER_MODEL")
            or ctx.config.get("orchestrator", {}).get("model", "local-orchestrator"))


def _litellm_base(ctx: ToolContext) -> str:
    return ctx.config.get("orchestrator", {}).get("litellm_base", "http://127.0.0.1:4000")


def _score_symbols(g: int) -> list[str]:
    """G single-token grade letters A, B, C, … (ordered worst→best)."""
    g = max(2, min(int(g), 26))
    return list(string.ascii_uppercase[:g])


def _expectation(top_logprobs: list | None, g: int) -> float | None:
    """Continuous score in [0,1] = Σ p(gradeᵢ)·valueᵢ over the grade tokens present
    in the first-token distribution. Robust to space-prefixed tokenizations. None
    if no grade token appears (the model didn't emit a grade)."""
    syms = _score_symbols(g)
    value = {s: i / (len(syms) - 1) for i, s in enumerate(syms)}
    mass: dict[str, float] = {}
    for e in top_logprobs or []:
        tok = (e.get("token") or "").strip().upper()
        if tok in value:
            mass[tok] = mass.get(tok, 0.0) + math.exp(e.get("logprob", -99.0))
    total = sum(mass.values())
    if total <= 0:
        return None
    return sum((p / total) * value[s] for s, p in mass.items())


def _grade_prompt(task: str, criterion: str, solution: str, g: int) -> str:
    syms = _score_symbols(g)
    return (
        f"You are a strict evaluator. Grade how well the SOLUTION satisfies the "
        f"CRITERION, on a {len(syms)}-level scale from {syms[0]} (does not satisfy at "
        f"all) to {syms[-1]} (fully satisfies).\n\n"
        f"TASK:\n{task or '(no task description given)'}\n\n"
        f"CRITERION:\n{criterion}\n\n"
        f"SOLUTION:\n{solution}\n\n"
        f"Reply with ONLY the single grade letter ({syms[0]}–{syms[-1]}). Grade:"
    )


async def _grade_once(client, base, key, model, prompt, g, temperature) -> float | None:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 1,
        "logprobs": True,
        "top_logprobs": max(20, g),
    }
    r = await client.post(f"{base}/v1/chat/completions", json=body,
                          headers={"Authorization": "Bearer " + key})
    r.raise_for_status()
    data = r.json()
    ch = (data.get("choices") or [{}])[0]
    content = ((ch.get("logprobs") or {}).get("content") or [])
    top = content[0].get("top_logprobs") if content else None
    return _expectation(top, g)


async def _score_solution(client, base, key, model, task, solution, criteria, g, k):
    """Returns (overall|None, {criterion: score|None})."""
    per: dict[str, float | None] = {}
    for crit in criteria:
        vals = []
        for i in range(k):
            temp = 0.0 if k == 1 else 0.7
            try:
                s = await _grade_once(client, base, key, model,
                                      _grade_prompt(task, crit, solution, g), g, temp)
            except Exception:
                s = None
            if s is not None:
                vals.append(s)
        per[crit] = (sum(vals) / len(vals)) if vals else None
    got = [v for v in per.values() if v is not None]
    return (sum(got) / len(got) if got else None), per


def _params(ctx: ToolContext, args: dict):
    vcfg = _vcfg(ctx)
    g = int(args.get("granularity") or vcfg.get("granularity", 20))
    k = max(1, int(args.get("repeats") or vcfg.get("repeats", 1)))
    criteria = args.get("criteria") or vcfg.get("criteria") or _DEFAULT_CRITERIA
    if isinstance(criteria, str):
        criteria = [criteria]
    model = _verifier_model(ctx, args.get("model"))
    return g, k, criteria, model


class VerifyScore(Tool):
    name = "verify.score"
    description = (
        "Score how well a solution satisfies a task/criteria, as a continuous number "
        "in [0,1] (1 = best). Uses an LLM verifier's logprobs (tie-free, calibrated), "
        "decomposed across criteria. Use it as a quality gate for tasks with NO "
        "external checker (summaries, reports, plans, research) — for code, run the "
        "tests instead. Returns the overall score and a per-criterion breakdown."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "solution": {"type": "string", "description": "The solution/answer text to grade."},
            "task": {"type": "string", "description": "What the solution was supposed to accomplish."},
            "criteria": {"type": "array", "items": {"type": "string"},
                         "description": "Criteria to grade against (default: correctness/completeness/quality)."},
            "model": {"type": "string", "description": "Verifier alias override (default: configured/env/brain)."},
            "granularity": {"type": "integer", "description": "G grade levels 2–26 (default 20)."},
            "repeats": {"type": "integer", "description": "K evals averaged (default 1)."},
        },
        "required": ["solution"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        sol = args.get("solution")
        if not sol:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="solution is required")
        g, k, criteria, model = _params(ctx, args)
        base, key = _litellm_base(ctx), os.environ.get("LITELLM_MASTER_KEY", "")
        async with httpx.AsyncClient(timeout=60) as client:
            overall, per = await _score_solution(client, base, key, model,
                                                 args.get("task", ""), sol, criteria, g, k)
        if overall is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"verifier '{model}' returned no gradable output — check it "
                                    "serves logprobs (llama.cpp n_probs / OpenAI top_logprobs).")
        return ToolResult(status="ok", tool_name=self.name, result={
            "score": round(overall, 4), "model": model, "granularity": g, "repeats": k,
            "per_criterion": {c: (round(v, 4) if v is not None else None) for c, v in per.items()}})


class VerifyRank(Tool):
    name = "verify.rank"
    description = (
        "Best-of-N: score several candidate solutions with the continuous verifier and "
        "rank them, returning the best. Use after generating multiple candidates (e.g. "
        "across the parallel brains) to pick the strongest — especially for tasks with "
        "no external checker. Returns candidates sorted best-first with their scores."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "candidates": {"type": "array", "items": {"type": "string"},
                           "description": "The candidate solution texts to rank."},
            "task": {"type": "string", "description": "What the candidates should accomplish."},
            "criteria": {"type": "array", "items": {"type": "string"}},
            "model": {"type": "string", "description": "Verifier alias override."},
            "granularity": {"type": "integer"},
            "repeats": {"type": "integer"},
        },
        "required": ["candidates"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        cands = args.get("candidates") or []
        if len(cands) < 2:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="need at least 2 candidates to rank")
        g, k, criteria, model = _params(ctx, args)
        base, key = _litellm_base(ctx), os.environ.get("LITELLM_MASTER_KEY", "")
        scored = []
        async with httpx.AsyncClient(timeout=60) as client:
            for idx, cand in enumerate(cands):
                overall, per = await _score_solution(client, base, key, model,
                                                     args.get("task", ""), cand, criteria, g, k)
                scored.append({"index": idx, "score": overall,
                               "per_criterion": {c: v for c, v in per.items()}})
        graded = [s for s in scored if s["score"] is not None]
        if not graded:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"verifier '{model}' returned no gradable output for any "
                                    "candidate — check it serves logprobs.")
        graded.sort(key=lambda s: s["score"], reverse=True)
        for s in graded:
            s["score"] = round(s["score"], 4)
            s["per_criterion"] = {c: (round(v, 4) if v is not None else None)
                                  for c, v in s["per_criterion"].items()}
        return ToolResult(status="ok", tool_name=self.name, result={
            "model": model, "granularity": g, "repeats": k,
            "best_index": graded[0]["index"], "best_score": graded[0]["score"],
            "ranked": graded})
