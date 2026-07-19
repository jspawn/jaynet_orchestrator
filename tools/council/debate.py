"""council.debate - multi-model deliberation across GPU 0, GPU 1, and optional cloud.

A panel of models reasons over a topic across several rounds. Round 1 is each
panelist's independent opening position; later rounds show each panelist the
others' takes so they can rebut, concede, and revise. The brain (orchestrator)
then synthesizes a structured result: each panelist's final position, a
consolidated pros/cons table, and a verdict.

Panelists run in parallel per round, so the two cards (brain on :8090, coder on
:8080) reason concurrently. Personas are passed per call. Model calls go straight
to LiteLLM (like eval.compare); each call's token usage IS charged to the run
budget (runtime.yaml `costs`), so a cloud panelist counts against max_cost_usd —
still keep rounds/panel small and mind cost.
"""

from __future__ import annotations

import asyncio
import os

import httpx

from runtime.tool_base import Tool, ToolContext, ToolResult


def _ccfg(ctx: ToolContext) -> dict:
    return ctx.config.get("council", {}) or {}


def _litellm_base(ctx: ToolContext) -> str:
    from runtime.paths import LITELLM_BASE
    return ctx.config.get("orchestrator", {}).get("litellm_base", LITELLM_BASE)


def _brain(ctx: ToolContext) -> str:
    return ctx.config.get("orchestrator", {}).get("model", "local-orchestrator")


def _normalize_panel(raw) -> list[dict]:
    """Accept ['alias', ...] or [{'model':.., 'persona':..}, ...] -> normalized dicts."""
    out = []
    for e in raw or []:
        if isinstance(e, str):
            out.append({"model": e, "persona": None})
        elif isinstance(e, dict) and e.get("model"):
            out.append({"model": e["model"], "persona": e.get("persona")})
    return out


def _label(p: dict, i: int) -> str:
    return (p.get("persona") or p["model"]) + f" (#{i + 1})"


async def _call(client, base, key, model, system, user, max_tokens) -> tuple[str, dict]:
    """One panelist/chair call. Returns (text, usage) so the caller can charge
    the run budget — the loop only sees the tool's envelope, not these side calls."""
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0.7,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    r = await client.post(f"{base}/v1/chat/completions", json=body,
                          headers={"Authorization": "Bearer " + key})
    r.raise_for_status()
    data = r.json()
    msg = ((data.get("choices") or [{}])[0]).get("message") or {}
    # reasoning models sometimes emit only reasoning_content if they run long
    text = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
    return text or "(no response)", (data.get("usage") or {})


def _charge(ctx: ToolContext, model: str, usage: dict) -> None:
    """Charge a direct-to-LiteLLM call to the run budget (mirrors eval.compare).
    Best-effort: a missing budget or cost row must never break a debate."""
    try:
        ptd = usage.get("prompt_tokens_details")
        cached = ptd.get("cached_tokens", 0) if isinstance(ptd, dict) else 0
        ctx.budget.add_usage(model, prompt=usage.get("prompt_tokens", 0),
                             completion=usage.get("completion_tokens", 0),
                             cached=cached, cost_table=ctx.config.get("costs", {}))
    except Exception:
        pass


def _panelist_system(persona: str | None) -> str:
    s = ("You are a panelist in a rigorous, good-faith multi-model deliberation. "
         "Reason carefully and argue your honest assessment - do not just agree.")
    if persona:
        s += f" Argue from this persona / viewpoint: {persona}."
    return s


def _opening_user(topic: str) -> str:
    return (f"TOPIC:\n{topic}\n\nGive your OPENING position. State your stance in a "
            "sentence, then your key reasoning, then the main PROS and CONS as you see "
            "them. Be substantive but concise.")


def _rebuttal_user(topic: str, others: str) -> str:
    return (f"TOPIC:\n{topic}\n\nThe other panelists said:\n\n{others}\n\nReconsider your "
            "position. Rebut where you disagree (say why), concede where they are right, "
            "and give your UPDATED stance with its pros and cons. Be concise.")


def _synth_user(topic: str, finals: list[tuple[str, str]]) -> str:
    block = "\n\n".join(f"[{lbl}]:\n{pos}" for lbl, pos in finals)
    return (f"TOPIC:\n{topic}\n\nThe panel's FINAL positions:\n\n{block}\n\nSynthesize this "
            "into a structured result with EXACTLY these three sections, in Markdown:\n"
            "1. **Positions** - one line per panelist, naming each and their stance.\n"
            "2. **Pros / Cons** - a consolidated two-column table of the strongest "
            "arguments for and against.\n"
            "3. **Verdict** - the bottom-line result and the single most important reason "
            "for it, plus any key caveat. Be neutral and decisive; don't just restate.")


class CouncilDebate(Tool):
    name = "council.debate"
    description = (
        "Convene a panel of models to deliberate a topic across rounds, then have the "
        "brain synthesize the result. Round 1 = independent opening positions; later "
        "rounds let each panelist see the others and rebut/revise. Returns each "
        "panelist's final position plus a structured pros/cons + verdict summary. "
        "Panelists run in parallel (GPU 0 brain + GPU 1 coder + optional cloud). Pass "
        "`panel` as aliases or {model, persona} objects to assign personas per call. "
        "Use for genuinely two-sided questions where independent viewpoints help - not "
        "for simple factual lookups."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "The question/topic to deliberate."},
            "panel": {
                "type": "array",
                "description": "Panelists: either model aliases (strings) or objects "
                               "{model, persona}. Defaults to the configured panel "
                               "(brain + coder).",
                "items": {"type": ["string", "object"]},
            },
            "rounds": {"type": "integer", "description": "Deliberation rounds (default 2, max 5). "
                                                         "Round 1 opens; the rest rebut."},
            "synthesizer": {"type": "string", "description": "Model that writes the final "
                                                             "summary (default: the brain)."},
            "max_tokens": {"type": "integer", "description": "Token cap per panelist per round "
                                                             "(default 1200)."},
        },
        "required": ["topic"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        topic = (args.get("topic") or "").strip()
        if not topic:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="topic is required")
        ccfg = _ccfg(ctx)
        panel = _normalize_panel(args.get("panel") or ccfg.get("panel")
                                 or [_brain(ctx), "local-coder"])
        if len(panel) < 2:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="need at least 2 panelists to deliberate")
        rounds = max(1, min(int(args.get("rounds", ccfg.get("rounds", 2))), 5))
        max_tokens = int(args.get("max_tokens", ccfg.get("max_tokens", 1200)))
        synth = args.get("synthesizer") or ccfg.get("synthesizer") or _brain(ctx)
        base, key = _litellm_base(ctx), os.environ.get("LITELLM_MASTER_KEY", "")

        labels = [_label(p, i) for i, p in enumerate(panel)]
        latest = [""] * len(panel)          # each panelist's most recent position
        transcript: list[list[dict]] = []   # per round: [{panelist, position}]

        async with httpx.AsyncClient(timeout=180) as client:
            for rnd in range(rounds):
                async def one(i):
                    p = panel[i]
                    sysmsg = _panelist_system(p.get("persona"))
                    if rnd == 0:
                        user = _opening_user(topic)
                    else:
                        others = "\n\n".join(f"[{labels[j]}]: {latest[j]}"
                                             for j in range(len(panel)) if j != i)
                        user = _rebuttal_user(topic, others)
                    try:
                        text, usage = await _call(client, base, key, p["model"],
                                                  sysmsg, user, max_tokens)
                        _charge(ctx, p["model"], usage)
                        return text
                    except Exception as e:
                        return f"(panelist error: {type(e).__name__})"
                results = await asyncio.gather(*[one(i) for i in range(len(panel))])
                latest = list(results)
                transcript.append([{"panelist": labels[i], "position": results[i]}
                                   for i in range(len(panel))])

            # brain synthesis over the final positions
            finals = list(zip(labels, latest))
            try:
                summary, usage = await _call(client, base, key, synth,
                                             "You are the neutral chair synthesizing a panel "
                                             "deliberation. Be structured and decisive.",
                                             _synth_user(topic, finals), max(max_tokens, 1200))
                _charge(ctx, synth, usage)
            except Exception as e:
                summary = f"(synthesis failed: {type(e).__name__}: {e})"

        return ToolResult(status="ok", tool_name=self.name, result={
            "topic": topic,
            "panel": [{"model": p["model"], "persona": p.get("persona"), "label": labels[i]}
                      for i, p in enumerate(panel)],
            "rounds": rounds,
            "synthesizer": synth,
            "final_positions": [{"panelist": lbl, "position": pos} for lbl, pos in finals],
            "synthesis": summary,
            "transcript": transcript,
        })
