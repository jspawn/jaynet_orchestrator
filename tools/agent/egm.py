"""Elephant–Goldfish review stages for the architect tool.

Every Goldfish is a fresh, tool-free child. The architect (Elephant) owns the
context and plan; reviewers only test whether that plan is understandable,
robust and sufficiently concrete to implement.
"""
from __future__ import annotations

import asyncio
import re


def parse_stance(text: str) -> str:
    match = re.search(r"STANCE:\s*(agree|refine|disagree)", text or "", re.I)
    return match.group(1).lower() if match else "refine"


def is_ready(text: str) -> bool:
    match = re.search(r"READY:\s*(yes|no)\b", text or "", re.I)
    return bool(match and match.group(1).lower() == "yes")


async def review(ctx, task: str, plan: str, model: str, mode: str,
                 count: int, emit) -> tuple[str, str, str]:
    """Run the selected review pipeline and return plan, evidence and stance."""
    if mode == "egm":
        comprehension = await comprehension_check(ctx, task, plan, model, emit)
        if not is_ready(comprehension):
            plan = await revise_plan(ctx, plan, comprehension)
        reviews = await antagonistic_reviews(ctx, task, plan, model, count, emit)
        text = ("COMPREHENSION:\n" + comprehension + "\n\n"
                + "\n\n".join(f"GOLDFISH {i}:\n{review}"
                              for i, review in enumerate(reviews, 1)))
        stances = {parse_stance(review) for review in reviews}
        stance = ("disagree" if "disagree" in stances else
                  "refine" if "refine" in stances else "agree")
        return plan, text, stance
    await emit("review")
    result = await ctx.spawn(
        "You are a senior engineer REVIEWING an architect's plan. Your job is to "
        "POKE HOLES — find flaws, missing cases, buildability problems, wrong "
        "assumptions — NOT to redesign. Answer in EXACTLY this format:\n"
        "STANCE: agree | refine | disagree\n"
        "NOTES: <specific holes / concerns; empty if none>\n"
        "ALTERNATIVE: <only if STANCE is disagree — your fundamentally different approach>\n\n"
        f"TASK:\n{task}\n\nPLAN:\n{plan}",
        tools=[], model=model, name="reviewer")
    text = (result.get("answer") or "").strip()
    return plan, text, parse_stance(text)


async def comprehension_check(ctx, task: str, plan: str, model: str, emit) -> str:
    await emit("comprehension")
    result = await ctx.spawn(
        "You are a stateless GOLDFISH reviewing an architect's plan. Do NOT "
        "critique or redesign yet. First prove comprehension: explain the "
        "architecture and intent back in your own words, then identify any "
        "ambiguity that prevents a shared understanding. Output EXACTLY:\n"
        "UNDERSTANDING: <faithful summary>\n"
        "READY: yes | no\n"
        "QUESTIONS: <ambiguities, or none>\n\n"
        f"TASK:\n{task}\n\nPLAN:\n{plan}",
        tools=[], model=model, name="goldfish-comprehension")
    return (result.get("answer") or "").strip()


async def antagonistic_reviews(ctx, task: str, plan: str, model: str,
                               count: int, emit) -> list[str]:
    async def one(index: int):
        result = await ctx.spawn(
            "You are an independent, stateless GOLDFISH acting as a skeptical "
            "senior engineer. Attack the plan, not the architect: find edge "
            "cases, hidden dependencies, wrong assumptions and buildability "
            "problems. Do NOT redesign unless you fundamentally disagree. "
            "Output EXACTLY:\nSTANCE: agree | refine | disagree\n"
            "NOTES: <specific holes; empty if none>\n"
            "ALTERNATIVE: <only for disagree — a fundamentally different approach>\n\n"
            f"TASK:\n{task}\n\nPLAN:\n{plan}",
            tools=[], model=model, name=f"goldfish-{index}")
        return (result.get("answer") or "").strip()

    await emit("goldfish", f"{count} independent reviews")
    return list(await asyncio.gather(*(one(i) for i in range(1, count + 1))))


async def readiness_check(ctx, task: str, plan: str, model: str, emit) -> str:
    await emit("readiness")
    result = await ctx.spawn(
        "You are the final stateless GOLDFISH implementation-readiness gate. "
        "Be strict. READY yes only when an executor can transcribe the plan "
        "without inventing architecture: exact file paths, data structures, "
        "function/API signatures where relevant, ordered units and concrete "
        "acceptance checks must be unambiguous. Output EXACTLY:\n"
        "READY: yes | no\nMISSING: <exact gaps, or none>\n\n"
        f"TASK:\n{task}\n\nPLAN:\n{plan}",
        tools=[], model=model, name="goldfish-readiness")
    return (result.get("answer") or "").strip()


async def revise_plan(ctx, plan: str, feedback: str) -> str:
    result = await ctx.spawn(
        "Revise your plan to resolve this Goldfish feedback. Keep the SAME "
        "labelled sections and make every implementation decision explicit; "
        "do not hide unresolved ambiguity behind generic wording.\n\n"
        f"PLAN:\n{plan}\n\nFEEDBACK:\n{feedback}",
        tools=[], name="architect")
    return (result.get("answer") or plan).strip()
