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


def _scale_symbols(scale: str, g: int):
    """(symbols, {symbol: value in [0,1]}). Default numeric 0-9 (higher = better) —
    single-token digits with no conflicting A=best prior. `letters` A.. is optional."""
    if scale == "letters":
        g = max(2, min(int(g), 26))
        syms = list(string.ascii_uppercase[:g])
    else:  # numeric 0-9
        syms = [str(d) for d in range(10)]
    n = len(syms)
    return syms, {sym: i / (n - 1) for i, sym in enumerate(syms)}


def _expectation(top_logprobs, values, min_mass: float = 0.5):
    """Continuous score in [0,1] IF grade tokens DOMINATE this position — i.e. their
    share of the position's (top-k) probability mass is >= min_mass. Else None.
    Dominance is what rejects an incidental digit/letter (an 'I' at p=0.0004, a '**'
    markdown token) from being misread as the grade; only a position whose top mass is
    actually a grade counts."""
    grade: dict[str, float] = {}
    total = 0.0
    for e in top_logprobs or []:
        p = math.exp(e.get("logprob", -99.0))
        total += p
        tok = (e.get("token") or "").strip()
        if tok in values:
            grade[tok] = grade.get(tok, 0.0) + p
    gm = sum(grade.values())
    if total <= 0 or gm / total < min_mass:
        return None
    return sum((p / gm) * values[sym] for sym, p in grade.items())


def _grade_prompt(task: str, criterion: str, solution: str, syms, no_think: bool = True) -> str:
    lo, hi = syms[0], syms[-1]
    numeric = lo.isdigit()
    if numeric:
        scale_desc = (f"a scale of {lo} to {hi}, where {lo} means it does not satisfy the "
                      f"criterion at all and {hi} means it fully satisfies it")
        unit = "digit"
    else:
        scale_desc = (f"a {len(syms)}-level scale from {lo} (does not satisfy at all) to "
                      f"{hi} (fully satisfies)")
        unit = "grade letter"
    prompt = (
        f"You are a strict evaluator. Grade how well the SOLUTION satisfies the CRITERION "
        f"on {scale_desc}.\n\n"
        f"TASK:\n{task or '(no task description given)'}\n\n"
        f"CRITERION:\n{criterion}\n\n"
        f"SOLUTION:\n{solution}\n\n"
        f"Reply with ONLY the single {unit} ({lo}-{hi}), nothing else. Grade:"
    )
    if no_think:
        prompt += " /no_think"   # Qwen3 soft switch: emit the grade directly, no reasoning
    return prompt


async def _grade_once(client, base, key, model, prompt, syms, values,
                      temperature, no_think=True, min_mass=0.5) -> float | None:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 8,          # allow a "**Grade:** " prefix; scan finds the grade token
        "logprobs": True,
        "top_logprobs": max(20, len(syms)),
    }
    if no_think:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    r = await client.post(f"{base}/v1/chat/completions", json=body,
                          headers={"Authorization": "Bearer " + key})
    r.raise_for_status()
    data = r.json()
    ch = (data.get("choices") or [{}])[0]
    content = ((ch.get("logprobs") or {}).get("content") or [])
    for pos in content:                          # first DOMINANT grade position
        s = _expectation(pos.get("top_logprobs"), values, min_mass)
        if s is not None:
            return s
    return None


async def _score_solution(client, base, key, model, task, solution, criteria,
                          syms, values, k, no_think=True, min_mass=0.5):
    """Returns (overall|None, {criterion: score|None})."""
    per: dict[str, float | None] = {}
    for crit in criteria:
        vals = []
        for i in range(k):
            temp = 0.0 if k == 1 else 0.7
            try:
                s = await _grade_once(client, base, key, model,
                                      _grade_prompt(task, crit, solution, syms, no_think),
                                      syms, values, temp, no_think, min_mass)
            except Exception:
                s = None
            if s is not None:
                vals.append(s)
        per[crit] = (sum(vals) / len(vals)) if vals else None
    got = [v for v in per.values() if v is not None]
    return (sum(got) / len(got) if got else None), per


def _params(ctx: ToolContext, args: dict):
    vcfg = _vcfg(ctx)
    scale = args.get("scale") or vcfg.get("scale", "numeric")
    g = int(args.get("granularity") or vcfg.get("granularity", 20))
    syms, values = _scale_symbols(scale, g)
    k = max(1, int(args.get("repeats") or vcfg.get("repeats", 1)))
    criteria = args.get("criteria") or vcfg.get("criteria") or _DEFAULT_CRITERIA
    if isinstance(criteria, str):
        criteria = [criteria]
    model = _verifier_model(ctx, args.get("model"))
    no_think = args.get("no_think", vcfg.get("no_think", True))
    min_mass = float(vcfg.get("min_grade_mass", 0.5))
    return syms, values, k, criteria, model, no_think, min_mass


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
        syms, values, k, criteria, model, no_think, min_mass = _params(ctx, args)
        base, key = _litellm_base(ctx), os.environ.get("LITELLM_MASTER_KEY", "")
        async with httpx.AsyncClient(timeout=60) as client:
            overall, per = await _score_solution(client, base, key, model,
                                                 args.get("task", ""), sol, criteria, syms, values, k, no_think, min_mass)
        if overall is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"verifier '{model}' returned no gradable output — check it "
                                    "serves logprobs (llama.cpp n_probs / OpenAI top_logprobs).")
        return ToolResult(status="ok", tool_name=self.name, result={
            "score": round(overall, 4), "model": model, "scale": ("numeric" if syms[0].isdigit() else "letters"), "levels": len(syms), "repeats": k,
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
        syms, values, k, criteria, model, no_think, min_mass = _params(ctx, args)
        base, key = _litellm_base(ctx), os.environ.get("LITELLM_MASTER_KEY", "")
        scored = []
        async with httpx.AsyncClient(timeout=60) as client:
            for idx, cand in enumerate(cands):
                overall, per = await _score_solution(client, base, key, model,
                                                     args.get("task", ""), cand, criteria, syms, values, k, no_think, min_mass)
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
            "model": model, "scale": ("numeric" if syms[0].isdigit() else "letters"), "levels": len(syms), "repeats": k,
            "best_index": graded[0]["index"], "best_score": graded[0]["score"],
            "ranked": graded})


class VerifyProbe(Tool):
    name = "verify.probe"
    description = (
        "Diagnostic for the verifier: send a prompt to the verifier model and return the "
        "raw first-token logprob distribution — the actual tokens it would emit, with "
        "probabilities — plus whether grade letters (A–T) appear and the continuous score "
        "they'd yield. Use this to confirm the verifier emits a GRADE as its first token "
        "(not reasoning). Runs IN-PROCESS through LiteLLM — key + network handled, no curl, "
        "no shell, no sandbox. If you see reasoning tokens (Here/Okay/Let/Thinking…) instead "
        "of letters, thinking isn't disabled on this model/template."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Prompt to send (default: a sample grade prompt)."},
            "model": {"type": "string", "description": "Verifier alias override."},
            "no_think": {"type": "boolean", "description": "Disable thinking (default: from verify.no_think)."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        vcfg = _vcfg(ctx)
        scale = args.get("scale") or vcfg.get("scale", "numeric")
        g = int(vcfg.get("granularity", 20))
        syms, values = _scale_symbols(scale, g)
        min_mass = float(vcfg.get("min_grade_mass", 0.5))
        model = _verifier_model(ctx, args.get("model"))
        no_think = args.get("no_think", vcfg.get("no_think", True))
        prompt = args.get("prompt") or _grade_prompt(
            "Name the capital of France.", "Correctness.", "Paris.", syms, no_think)
        base, key = _litellm_base(ctx), os.environ.get("LITELLM_MASTER_KEY", "")
        body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0, "max_tokens": 8, "logprobs": True, "top_logprobs": max(20, len(syms))}
        if no_think:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(f"{base}/v1/chat/completions", json=body,
                                      headers={"Authorization": "Bearer " + key})
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"probe call to '{model}' failed: {type(e).__name__}: {e}")
        ch = (data.get("choices") or [{}])[0]
        content = ((ch.get("logprobs") or {}).get("content") or [])
        positions, grade_pos, score = [], None, None
        for i, pos in enumerate(content):
            top = [{"token": e.get("token"),
                    "p": round(math.exp(e.get("logprob", -99.0)), 4)}
                   for e in (pos.get("top_logprobs") or [])[:12]]
            positions.append({"emitted": pos.get("token"), "top": top})
            if grade_pos is None:
                s = _expectation(pos.get("top_logprobs"), values, min_mass)   # dominant only
                if s is not None:
                    grade_pos, score = i, round(s, 4)
        ok = grade_pos is not None
        return ToolResult(status="ok", tool_name=self.name, result={
            "model": model, "scale": scale, "no_think": no_think,
            "reasoning_content": (ch.get("message") or {}).get("reasoning_content"),
            "grade_found_at_position": grade_pos, "continuous_score": score,
            "verdict": (f"OK — a {syms[0]}-{syms[-1]} grade dominates position {grade_pos}; "
                        "verify.score will work" if ok else
                        "NO grade letter in the first tokens — thinking is likely still on, or "
                        "the grade isn't a single token. verify.score would return "
                        "'no gradable output' as-is."),
            "positions": positions})
