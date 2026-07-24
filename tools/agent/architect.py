"""architect — plan-first handler for a complex task.

Runs a five-stage pipeline, all as budget-bounded sub-agents over ctx.spawn:

  1. PLAN      the brain does a planning + architecture pass (read-only tools).
  2. REVIEW    the specialist model pokes holes in the plan and returns an explicit
               verdict: agree / refine / disagree.
  3. ARBITRATE only if the reviewer *disagrees*: a cloud model is shown both
               approaches and picks one (or a hybrid) with a short rationale.
               If arbitration itself fails (unknown alias, cloud error), the
               handoff says so explicitly and defaults to the plan — it never
               silently pretends the reviewer agreed.
  4. HANDOFF   the final approach is distilled into a HANDOFF.md.
  5. EXECUTE   a FRESH agent, seeded only with the handoff (clean context, no
               planning chatter), carries the plan out unit-by-unit.

The orchestrator calls this when it has judged a request complex enough to
warrant planning first (see the complexity threshold in the prompt). Simple
requests should NOT use this — just do the work.
"""

from __future__ import annotations

import re

from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.agent.spawn import _resolve_spawn_model

_PLAN_TOOLS = ["fs.read", "fs.list", "fs.grep", "code.tree", "code.symbols",
               "web.search", "web.fetch", "rag.search", "memory.search", "arxiv.search"]
_EXEC_TOOLS = ["fs.read", "fs.list", "fs.grep", "fs.write", "fs.edit",
               "code.run", "code.symbols", "code.tree", "code.patch", "code.deps",
               "lint.run", "test.run", "git.status", "git.diff", "git.add", "git.commit",
               "skill.load", "skill.list", "note.set", "context.pin"]


def _parse_stance(text: str) -> str:
    m = re.search(r"STANCE:\s*(agree|refine|disagree)", text or "", re.I)
    return m.group(1).lower() if m else "refine"   # unclear → treat as refine (safe middle)


def _parse_choice(text: str) -> str:
    m = re.search(r"CHOICE:\s*(A|B|hybrid)", text or "", re.I)
    return m.group(1).upper() if m else "A"


def _section(text: str, label: str) -> str:
    """Pull the body after 'LABEL:' up to the next ALL-CAPS label or end."""
    m = re.search(label + r":\s*(.*?)(?=\n[A-Z][A-Z ]{2,}:|\Z)", text or "", re.S | re.I)
    return (m.group(1).strip() if m else "")


class Architect(Tool):
    name = "architect"
    description = (
        "Plan-first handler for a COMPLEX task. Runs a planning + architecture "
        "pass, has the specialist model poke holes in the plan, escalates to a cloud "
        "model ONLY if they fundamentally disagree, writes a HANDOFF.md, then "
        "executes the plan in a FRESH context seeded only with that handoff. Use "
        "it when you've judged a request complex enough to plan first (a "
        "multi-file build, a non-trivial refactor, an ambiguous design task). Do "
        "NOT use it for simple requests — just do those directly. Pass a complete, "
        "standalone task; this agent shares none of the current conversation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string",
                     "description": "The full, standalone task to plan and carry out."},
            "execute": {"type": "boolean",
                        "description": "Carry out the plan after the handoff (default true). "
                                       "False = produce the plan + HANDOFF.md only."},
        },
        "required": ["task"],
    }
    private = True

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        task = (args.get("task") or "").strip()
        if not task:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="task is required")
        if getattr(ctx, "spawn", None) is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="architect needs sub-agent spawning, which isn't available here")
        do_exec = args.get("execute", True)
        cfg = (ctx.config.get("architect") or {})
        # Stage models resolve like agent.spawn's: friendly aliases (gemini) map
        # to their LiteLLM alias, live serve.start'd aliases also resolve. An
        # unresolvable reviewer can't do its job — fail fast and loud rather
        # than discovering mid-pipeline.
        reviewer_cfg = cfg.get("reviewer_model", "local-specialist")
        arbiter_cfg = cfg.get("arbiter_model", "kimi")
        reviewer = _resolve_spawn_model(reviewer_cfg, ctx)
        if reviewer is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"architect.reviewer_model {reviewer_cfg!r} doesn't "
                                    "resolve to a known LiteLLM alias — fix the value "
                                    "in config/runtime.yaml")

        _STAGE = {"plan": "Planning the approach …",
                  "review": "Coder poking holes in the plan …",
                  "arbitrate": "Reviewer disagreed — asking a cloud model to decide …",
                  "refine": "Folding the review into the plan …",
                  "execute": "Executing the plan in a fresh context …"}

        async def emit(stage, info=""):
            # ctx.emit is the 2-arg tool seam; emit a 'progress' event the UI renders
            # live under the running architect call.
            try:
                if getattr(ctx, "emit", None):
                    await ctx.emit("progress", {"label": _STAGE.get(stage, stage)})
            except Exception:
                pass

        # ---- 1. PLAN ----
        await emit("plan")
        plan = await ctx.spawn(
            "You are the ARCHITECT. Produce a concrete plan and architecture for the "
            "task below. Investigate as needed (read files, search), but do NOT write "
            "the final implementation. Output EXACTLY these labelled sections:\n"
            "GOAL: <one line>\nAPPROACH: <the approach and why>\n"
            "KEY DECISIONS: <bullets>\nRISKS: <bullets>\n"
            "UNITS: <ordered small steps; for each: what, which files, and a done-check>\n\n"
            f"TASK:\n{task}",
            tools=cfg.get("plan_tools") or _PLAN_TOOLS, name="architect")
        plan_text = (plan.get("answer") or "").strip()
        if plan.get("status") not in ("ok", None) or not plan_text:
            return ToolResult(status="error", result={"stage": "plan", "detail": plan},
                              tool_name=self.name, error="planning stage failed")

        # ---- 2. REVIEW (poke holes) ----
        await emit("review")
        review = await ctx.spawn(
            "You are a senior engineer REVIEWING an architect's plan. Your job is to "
            "POKE HOLES — find flaws, missing cases, buildability problems, wrong "
            "assumptions — NOT to redesign. Answer in EXACTLY this format:\n"
            "STANCE: agree | refine | disagree\n"
            "NOTES: <specific holes / concerns; empty if none>\n"
            "ALTERNATIVE: <only if STANCE is disagree — your fundamentally different approach>\n\n"
            f"TASK:\n{task}\n\nPLAN:\n{plan_text}",
            tools=[], model=reviewer, name="reviewer")
        review_text = (review.get("answer") or "").strip()
        stance = _parse_stance(review_text)

        # ---- 3. ARBITRATE (only on genuine disagreement) or REFINE ----
        arbitration = None
        arb_error = None
        final_plan = plan_text
        if stance == "disagree":
            arbiter = _resolve_spawn_model(arbiter_cfg, ctx)
            if arbiter is None:
                arb_error = (f"architect.arbiter_model {arbiter_cfg!r} doesn't resolve "
                             "to a known LiteLLM alias — fix config/runtime.yaml")
                await emit("arbitrate", arb_error)
            else:
                await emit("arbitrate", f"reviewer disagreed → {arbiter}")
                arb = await ctx.spawn(
                    "Two experts proposed different approaches to the same task. Pick ONE "
                    "to proceed with (or a clear hybrid) and justify briefly. Do NOT write "
                    "a new plan from scratch. Answer:\nCHOICE: A | B | hybrid\n"
                    "RATIONALE: <2-4 sentences>\n\n"
                    f"TASK:\n{task}\n\nPLAN A (architect):\n{plan_text}\n\n"
                    f"PLAN B (reviewer's alternative):\n{_section(review_text, 'ALTERNATIVE') or review_text}",
                    tools=[], model=arbiter, name="arbiter")
                answer = (arb.get("answer") or "").strip()
                if arb.get("status") != "ok" or not answer:
                    arb_error = ("arbiter call failed: "
                                 f"{arb.get('error') or arb.get('status') or 'empty answer'}")
                    await emit("arbitrate", arb_error)
                else:
                    arbitration = answer
        elif stance == "refine":
            await emit("refine", "folding review notes into the plan")
            rev = await ctx.spawn(
                "Revise your plan to address this review. Keep the SAME labelled "
                "sections (GOAL/APPROACH/KEY DECISIONS/RISKS/UNITS).\n\n"
                f"PLAN:\n{plan_text}\n\nREVIEW:\n{review_text}",
                tools=[], name="architect")
            final_plan = (rev.get("answer") or plan_text).strip()

        # ---- 4. HANDOFF ----
        handoff = self._build_handoff(task, final_plan, review_text, stance,
                                      arbitration, arb_error)

        # ---- 5. EXECUTE in a fresh context (seeded only with the handoff) ----
        if not do_exec:
            return ToolResult(status="ok", tool_name=self.name, result={
                "stance": stance, "arbitrated": arbitration is not None,
                "arbitration_error": arb_error,
                "executed": False, "handoff": handoff, "answer": final_plan})

        await emit("execute")
        result = await ctx.spawn(
            "You are executing a pre-approved plan in a FRESH context. First save the "
            "handoff below verbatim to HANDOFF.md. Then carry it out unit by unit, "
            "running each unit's done-check before moving on. If something in the plan "
            "proves wrong, note it and adapt — don't blindly follow a broken step.\n\n"
            "=== HANDOFF ===\n" + handoff,
            tools=cfg.get("exec_tools") or _EXEC_TOOLS, name="executor",
            verify=cfg.get("verify"))

        return ToolResult(status="ok", tool_name=self.name, result={
            "stance": stance,
            "arbitrated": arbitration is not None,
            "arbitration_error": arb_error,
            "executed": True,
            "execution_status": result.get("status"),
            "verified": result.get("verified"),
            "files_changed": result.get("files_changed") or [],
            "handoff": handoff,
            "answer": result.get("answer") or final_plan,
        })

    @staticmethod
    def _build_handoff(task, final_plan, review_text, stance, arbitration,
                       arb_error=None) -> str:
        parts = ["# HANDOFF\n", "## Task\n" + task + "\n", "## Plan\n" + final_plan + "\n"]
        if stance == "disagree" and arbitration:
            choice = _parse_choice(arbitration)
            parts.append("## Arbitration\nThe reviewer disagreed; a cloud model chose "
                         f"**approach {choice}**.\n\n" + arbitration +
                         "\n\nFollow the chosen approach above.\n")
        elif stance == "disagree":
            # Loud fallback: never pretend the reviewer agreed. The executor gets
            # the plan AND the live dissent, so it can re-plan instead of forcing
            # through a step the reviewer predicted would fail.
            parts.append("## Arbitration\nThe reviewer DISAGREED with the plan, but "
                         f"cloud arbitration failed ({arb_error or 'no arbiter available'}), "
                         "so no ruling was made. Proceed with the plan above by default, "
                         "but treat the reviewer's dissent as live: where a unit hits the "
                         "problem the reviewer predicted, stop and re-plan rather than "
                         "forcing through.\n\nREVIEW:\n" + review_text + "\n")
        elif stance == "refine":
            parts.append("## Review\nThe plan was refined to address the reviewer's notes:\n\n"
                         + review_text + "\n")
        else:
            parts.append("## Review\nThe reviewer agreed with the plan.\n")
        parts.append("## Execution\nWork through the UNITS in order. Run each unit's "
                     "done-check before proceeding. Keep context lean.\n")
        return "\n".join(parts)
