"""council.vote — self-consistency: ask the same model N times, majority-vote.

Small local models are coin-flippy on questions that have ONE right answer
(math, counting, puzzles, exact lookups): any single sample may be wrong, but
the correct answer is usually the MODE across independent samples at
temperature. This tool samples the same question N times in parallel,
extracts each reply's final answer line, and returns the majority winner with
the full vote distribution — the brain gets ground-truth-ish signal instead
of one shaky sample.

Deliberately simple voting: normalized exact match on the extracted answer
(lowercase, whitespace/punctuation collapsed). Semantically-equal-but-worded-
differently answers do NOT cluster — that's what council.debate's synthesis
is for. This tool is for verifiable single-answer questions only.

Model calls go straight to LiteLLM (like council.debate / eval.compare) and
each sample IS charged to the run budget: N samples cost N completions, so
keep N small. Cloud gate mirrors council.debate (audit S1): a non-local
`model` sends the question off-box — human approval when
confirmation.confirm_cloud_calls is on, refused outright on a private-tainted
run without 'share with cloud'.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import Counter

import httpx

from runtime import cloud_gate
from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.council.debate import _brain, _charge, _litellm_base

_N_DEFAULT, _N_MAX = 5, 9
_SAMPLE_PREVIEW = 200      # chars of each sample's reasoning kept for the brain

_ANSWER_RE = re.compile(r"^\s*ANSWER:\s*(.+?)\s*$", re.I | re.M)
_NORM_RE = re.compile(r"[^a-z0-9.%-]+")

_SYSTEM = ("Answer the question precisely and independently. Reason step by "
           "step, then end your reply with a final line of exactly this form:\n"
           "ANSWER: <your final answer>")


def _extract(text: str) -> str:
    """The final answer line; falls back to the last non-empty line."""
    matches = _ANSWER_RE.findall(text or "")
    if matches:
        return matches[-1]
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _normalize(answer: str) -> str:
    """Comparison key: case/punctuation/whitespace-insensitive, so 'Paris',
    'paris.' and ' Paris ' cluster. Inner dots survive (16.5 != 165);
    edge dots are stripped. Kept dumb on purpose — semantic clustering
    is the debate synthesizer's job, not a counter's."""
    return _NORM_RE.sub(" ", (answer or "").lower()).strip().strip(".").strip()


class CouncilVote(Tool):
    name = "council.vote"
    description = (
        "Self-consistency voting: ask ONE model the same question n times in "
        "parallel at temperature and majority-vote the extracted answers. "
        "Reliably lifts small local models on questions with a SINGLE "
        "verifiable answer (math, counting, puzzles, dates, exact lookups) — "
        "any one sample may be wrong, but the right answer is usually the "
        "mode. Returns the winner, the vote distribution, and a preview of "
        "each sample. Costs n completions (charged to your budget), so use it "
        "deliberately — never for open-ended writing, opinions, or "
        "long-form (no meaningful majority exists there; use council.debate "
        "for two-sided judgment calls). A cloud (non-local) model sends the "
        "question off-box: needs human approval when confirm_cloud_calls is "
        "on, refused when the conversation holds private tool results without "
        "'share with cloud' — use local aliases for private questions."
    )
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string",
                         "description": "The single-answer question to vote on."},
            "n": {"type": "integer",
                  "description": f"Samples taken (default {_N_DEFAULT}, max "
                                 f"{_N_MAX}). Odd numbers avoid ties; each "
                                 "sample is a billed completion."},
            "model": {"type": "string",
                      "description": "Model alias to sample (default: the "
                                     "brain). Local aliases keep the question "
                                     "on-box; cloud aliases gate (see above)."},
            "temperature": {"type": "number",
                            "description": "Sampling temperature (default 0.8). "
                                           "Needs to be >0 — identical greedy "
                                           "samples make voting pointless."},
            "max_tokens": {"type": "integer",
                           "description": "Token cap per sample (default 600)."},
        },
        "required": ["question"],
    }

    def _model(self, args: dict, ctx: ToolContext) -> str:
        return (args.get("model")
                or (ctx.config.get("council") or {}).get("vote_model")
                or _brain(ctx))

    def needs_confirmation(self, args: dict, context: ToolContext) -> bool:
        # Same cloud rule as council.debate (audit S1): local models never gate.
        if not cloud_gate.confirm_cloud_enabled(context.config):
            return False
        return bool(cloud_gate.cloud_targets([self._model(args, context)],
                                             context.config))

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        question = (args.get("question") or "").strip()
        if not question:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="question is required")
        model = self._model(args, ctx)
        refusal = cloud_gate.privacy_refusal(ctx, [model])
        if refusal:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=refusal)
        try:
            n = int(args.get("n", _N_DEFAULT))
        except (TypeError, ValueError):
            n = _N_DEFAULT
        n = max(1, min(n, _N_MAX))
        try:
            temperature = float(args.get("temperature", 0.8))
        except (TypeError, ValueError):
            temperature = 0.8
        max_tokens = int(args.get("max_tokens", 600) or 600)
        base, key = _litellm_base(ctx), os.environ.get("LITELLM_MASTER_KEY", "")

        async with httpx.AsyncClient(timeout=180) as client:
            async def one(_i):
                try:
                    body_temp = {"temperature": temperature}
                    # _call pins temperature=0.7; voting wants real spread, so
                    # call the endpoint directly with the requested value.
                    body = {"model": model, "max_tokens": max_tokens,
                            **body_temp,
                            "messages": [{"role": "system", "content": _SYSTEM},
                                         {"role": "user", "content": question}]}
                    r = await client.post(f"{base}/v1/chat/completions",
                                          json=body,
                                          headers={"Authorization":
                                                   "Bearer " + key})
                    r.raise_for_status()
                    data = r.json()
                    msg = ((data.get("choices") or [{}])[0]).get("message") or {}
                    text = ((msg.get("content") or "").strip()
                            or (msg.get("reasoning_content") or "").strip())
                    _charge(ctx, model, data.get("usage") or {})
                    return text or "(no response)"
                except Exception:
                    # A failed sample abstains — it must not vote.
                    return None
            samples = await asyncio.gather(*[one(i) for i in range(n)])

        valid = [s for s in samples if s is not None]
        extracted = [_extract(s) for s in valid]
        votes: Counter = Counter()
        by_norm: dict[str, str] = {}     # norm -> first surface form
        for a in extracted:
            norm = _normalize(a)
            if not norm:
                continue
            by_norm.setdefault(norm, a)
            votes[norm] += 1
        if not votes:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="no sample produced an extractable answer")
        sample_errors = len(samples) - len(valid)
        top = votes.most_common()
        best_count = top[0][1]
        tied = [norm for norm, c in top if c == best_count]
        winner_norm = tied[0] if len(tied) == 1 else None

        return ToolResult(status="ok", tool_name=self.name, result={
            "question": question,
            "model": model,
            "n": n,
            "temperature": temperature,
            "winner": (by_norm[winner_norm] if winner_norm else None),
            "winner_votes": best_count if winner_norm else 0,
            "tie": winner_norm is None,
            "tied_answers": ([by_norm[t] for t in tied]
                             if winner_norm is None else []),
            "votes": {by_norm[norm]: c for norm, c in top},
            "sample_errors": sample_errors,
            "samples": [{"answer": a, "preview": s[:_SAMPLE_PREVIEW]}
                        for a, s in zip(extracted, valid)],
        })
