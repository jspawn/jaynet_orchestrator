"""Agent reasoning loop.

A bounded Level-1 agent: model proposes tool calls, runtime executes them,
results fed back, repeats until the model produces a final answer or any
budget ceiling is hit.

Key responsibilities:
- Translate between OpenAI tool-call format and our ToolResult envelope
- Enforce privacy: block private tool results from being passed to remote LLMs
- Detect repeat tool calls (same name+args twice) → loop guard
- Update budget on every model turn and tool call
- Log every step to the trace DB
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

from .budget import Budget, BudgetExceeded
from .registry import ToolRegistry
from .selector import ToolSelector
from .skills import discover_skills, render_catalog
from .tool_base import Tool, ToolContext, ToolResult
from .trace import Trace

log = logging.getLogger(__name__)

# Qwen3-family brains wrap chain-of-thought in <think>…</think>. That reasoning
# must never reach the user's answer, the conversation history, or the trace as
# answer text — it belongs in the UI's collapsible "thinking" view (routed live
# via the "reasoning" token scope). These helpers strip/split it.
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _budget_warning(pressure: float, dim: str) -> str:
    """The checkpoint nudge injected once the run nears a ceiling."""
    return (
        f"\u26a0 BUDGET NOTICE: this run has used about {int(pressure * 100)}% of its "
        f"{dim} budget and will be cut off when it hits the limit. Do NOT start new work "
        f"or spawn new sub-tasks. Land the plane now:\n"
        f"1. Finish the current step only if it's nearly done.\n"
        f"2. Save in-progress work to the project (fs.write / deliver.files) so nothing is lost.\n"
        f"3. Write or update NEXT_STEPS.md in the project: what's done, what remains, and how "
        f"to resume in a fresh run.\n"
        f"4. Give the user a short summary of where things stand, then stop.\n"
        f"A clean hand-off beats squeezing in one more change."
    )


def _strip_think(text: str) -> str:
    """Remove complete <think>…</think> blocks from a finished string. Used on the
    non-streaming path and as a safety net on the assembled streaming content."""
    if not text or _THINK_OPEN not in text:
        return text
    return _THINK_RE.sub("", text).strip()


def _suffix_prefix_len(s: str, tag: str) -> int:
    """Longest suffix of s that is a proper prefix of tag — i.e. how many trailing
    chars to hold back in case a tag is split across streamed chunks."""
    for k in range(min(len(s), len(tag) - 1), 0, -1):
        if s[-k:] == tag[:k]:
            return k
    return 0


def _traj_arg_hint(args: dict) -> str:
    """A short, non-sensitive hint of what a tool call was aimed at — taken from
    the call's *arguments* (the model's own inputs: a URL, query, path, model,
    collection), never from the result, so trajectory notes can't leak private
    tool output back into replayed history."""
    for k in ("url", "query", "path", "task", "model", "collection", "name"):
        v = args.get(k)
        if v:
            s = str(v).replace("\n", " ").strip()
            return s[:70] + ("…" if len(s) > 70 else "")
    return ""


def _traj_entry(name: str, args: dict, result) -> str:
    """One compact trajectory line: tool(hint)->status[: error]."""
    hint = _traj_arg_hint(args)
    head = f"{name}({hint})" if hint else name
    if result.status == "ok":
        return f"{head}→ok"
    return f"{head}→{result.status}: {(result.error or '')[:80]}"


def _format_trajectory(entries: list[str]) -> str:
    """Assemble a budget-friendly summary of the run's tool calls (most recent
    kept), folded into the saved answer so a follow-up turn knows what was tried."""
    if not entries:
        return ""
    s = "; ".join(entries[-14:])
    return s[:800] + ("…" if len(s) > 800 else "")


class PrivacyViolation(Exception):
    """Orchestrator tried to pass private content to a remote LLM tool."""

class _NestedConfirm:
    """Routes a sub-agent's confirmation request up to the parent run, so a
    child's confirmation-gated tool (e.g. fs.write) still prompts the human on
    the parent's live stream, against the parent's run_id."""

    def __init__(self, provider, parent_emit, parent_run_id: str):
        self._provider = provider
        self._emit = parent_emit
        self._run_id = parent_run_id

    async def confirm(self, run_id: str, name: str, args: dict, emit) -> bool:
        # Ignore the child's run_id/emit; use the parent's so the request and the
        # eventual /approve line up with what the UI is already listening to.
        return await self._provider.confirm(self._run_id, name, args, self._emit)


class AgentRuntime:
    def __init__(self, config_path: str | Path = "/srv/orchestrator/config/runtime.yaml"):
        self.config_path = Path(config_path)
        with self.config_path.open() as f:
            self.config = yaml.safe_load(f)

        orch_root = self.config_path.parent.parent
        tools_root = orch_root / "tools"
        self.registry = ToolRegistry(tools_root)
        self.registry.discover()
        log.info("Discovered %d tools: %s",
                 len(self.registry.all()),
                 ", ".join(sorted(t.name for t in self.registry.all())))

        # Mark tools private based on namespace config
        private_ns = set(self.config["privacy"]["private_tool_namespaces"])
        for tool in self.registry.all():
            ns = tool.name.split(".", 1)[0]
            if ns in private_ns:
                tool.private = True

        prompt_path = orch_root / self.config["orchestrator"]["system_prompt"]
        self.system_prompt = prompt_path.read_text(encoding="utf-8", errors="replace")

        # Runtime-loadable skills: discover once, inject the lightweight catalog
        # into the system prompt so the model knows what it can load on demand.
        sk_cfg = self.config.get("skills", {}) or {}
        self.skills = discover_skills(sk_cfg.get("dir", str(orch_root / "skills")))
        self.skill_catalog = render_catalog(self.skills)

        self.trace = Trace(
            self.config["trace"]["db_path"],
            log_content=self.config["trace"]["log_content"],
        )

        self.litellm_base = self.config["orchestrator"]["litellm_base"]
        self.model = self.config["orchestrator"]["model"]

        # Brain identity + capabilities, optionally read from the llama-serve.sh
        # preset that's currently serving the brain. The orchestrator talks to the
        # brain via LiteLLM (self.model stays the LiteLLM alias); the preset only
        # tells us *what* is loaded — notably whether it can see images.
        orch_cfg = self.config["orchestrator"]
        self.brain_info: dict = {}
        # ORCH_BRAIN_PRESET (env) overrides runtime.yaml's brain_preset, so the same
        # orchestrator.env that drives the serving scripts can also point JayNet at
        # the active preset. Empty/unset env falls back to the YAML value.
        preset_path = os.environ.get("ORCH_BRAIN_PRESET") or orch_cfg.get("brain_preset")
        if preset_path:
            from runtime.serve_preset import preset_info
            self.brain_info = preset_info(preset_path)
        vis_override = orch_cfg.get("vision")  # null=auto, true/false=force
        if vis_override is None:
            self.vision_enabled = bool(self.brain_info.get("vision"))
        else:
            self.vision_enabled = bool(vis_override)
        self.cost_table = self.config["costs"]
        self.selector = ToolSelector(self.registry, self.config)

    async def run(self, user_message: str, *, share_private: bool = False,
                  budget_overrides: dict | None = None,
                  tools: list[str] | None = None,
                  auto_confirm: bool = False,
                  run_id: str | None = None,
                  on_event=None,
                  confirm_provider=None,
                  history: list[dict] | None = None,
                  model: str | None = None,
                  depth: int = 0,
                  owner: str | None = None,
                  think: bool = True,
                  extra_system: str | None = None,
                  images: list[str] | None = None,
                  stream: bool = False) -> dict:
        """Execute one full agent run. Returns a result dict with answer + metadata.

        on_event:  optional async callable(event: dict) — receives every step as
                   a transport-neutral event. The CLI passes nothing; the web
                   layer passes a bus publisher. Never imported here.
        confirm_provider: optional object with `async confirm(run_id, tool, args,
                   emit) -> bool`. If None, falls back to the TTY/non_interactive
                   prompt (unchanged CLI behaviour).
        history:   optional prior turns as [{role, content}, ...], inserted after
                   the system prompt and before this message — multi-turn memory.
                   The cacheable system+tools prefix stays first; history slots in
                   after it. Cost grows with history length (caller's choice).
        model:     optional brain override for THIS run (e.g. a sub-agent on a
                   different model alias). Defaults to the configured brain.
        depth:     sub-agent nesting depth. 0 = top-level. Children spawned via
                   ctx.spawn run at depth+1, capped by config agent.max_depth.
        stream:    if True, the brain's model turns stream token-by-token (and
                   token/cost events are emitted). The CLI leaves this False to
                   keep the proven non-streaming path.
        """
        run_id = run_id or str(uuid.uuid4())
        eff_model = model or self.model
        b_cfg = {**self.config["budgets"], **(budget_overrides or {})}
        warn_fraction = float(b_cfg.get("warn_fraction", 0.8) or 0)
        budget = Budget(
            max_iterations=b_cfg["max_iterations"],
            max_wall_clock_s=b_cfg["max_wall_clock_s"],
            max_cost_usd=b_cfg["max_cost_usd"],
            max_total_tokens=b_cfg["max_total_tokens"],
        )

        self.trace.start_run(run_id, user_message, owner=owner)

        # Single emit seam: writes to the trace AND (if present) to the event
        # sink. Every step in the loop goes through this, so the trace and the
        # live stream never diverge.
        _seq = {"n": 0}

        async def emit(event_type: str, iteration: int, data: dict) -> None:
            self.trace.log(run_id, event_type, iteration, data)
            if on_event is not None:
                _seq["n"] += 1
                try:
                    await on_event({
                        "v": 1, "run_id": run_id, "seq": _seq["n"],
                        "ts": time.time(), "type": event_type,
                        "iteration": iteration, "data": data,
                    })
                except Exception:
                    log.exception("on_event sink raised (continuing)")

        await emit("run_start", 0, {"message": user_message,
                                    "share_private": share_private})

        system_content = self.system_prompt
        if self.skill_catalog:
            system_content += "\n\n" + self.skill_catalog
        if extra_system:
            system_content += "\n\n" + extra_system
        messages: list[dict] = [{"role": "system", "content": system_content}]
        # Prior turns (multi-turn memory) go after the system prompt so the
        # cacheable system+tools prefix is undisturbed. Only user/assistant text
        # turns are replayed — not the internal tool-call transcript.
        for h in (history or []):
            role = h.get("role")
            if role in ("user", "assistant") and h.get("content"):
                content = h["content"]
                # If a prior assistant turn carried a trajectory note, replay it so
                # a follow-up ("try again", "continue") knows what was already tried.
                if role == "assistant" and h.get("trajectory"):
                    content = f"{content}\n\n[Tools you ran that turn: {h['trajectory']}]"
                messages.append({"role": role, "content": content})
        if images and self.vision_enabled:
            # OpenAI/LiteLLM multimodal: content becomes a list of blocks. The
            # text part stays first; each image rides as an image_url block. The
            # plain string `user_message` is still used for the trace, the
            # run_start event, and tool selection below.
            content_blocks: list[dict] = [{"type": "text", "text": user_message}]
            for url in images:
                content_blocks.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": "user", "content": content_blocks})
        else:
            messages.append({"role": "user", "content": user_message})
        # Track which assistant messages were derived from private tool results.
        # Indexed by message position. Used to enforce privacy on subsequent calls.
        private_taint: set[int] = set()
        # Track recent tool calls for loop detection.
        recent_calls: list[str] = []
        # Compact record of what this run did, folded into the answer so a
        # follow-up turn has the trajectory (not just the final text).
        trajectory: list[str] = []

        # Select tools ONCE, before the loop starts, and freeze the set for the
        # whole run. The tool schemas are a stable prefix; keeping them constant
        # is what preserves prompt-cache hits across iterations (see guide §3.7).
        allowed = self.selector.select(user_message, requested=tools)
        tools_schema = self.registry.openai_schemas(allowed)
        await emit("tool_selection", 0, {
            "mode": self.selector.mode,
            "requested": tools,
            "selected": allowed if allowed is not None else "all",
            "count": len(tools_schema),
        })

        # Token emitter: forwards streamed deltas as `token` events. scope is
        # "brain" for the orchestrator model, or a tool name (e.g. "llm.call").
        async def emit_token(text: str, scope: str = "brain", model: str | None = None):
            if on_event is not None and text:
                _seq["n"] += 1
                await on_event({
                    "v": 1, "run_id": run_id, "seq": _seq["n"], "ts": time.time(),
                    "type": "token", "iteration": 0,
                    "data": {"scope": scope, "model": model, "text": text},
                })

        # Live cost meter: emit running total after each usage charge.
        async def emit_cost(model: str, delta: float):
            await emit("cost", budget.iterations, {
                "model": model, "delta_usd": round(delta, 6),
                "total_usd": round(budget.cost_usd, 6),
                "total_tokens": budget.total_tokens,
            })

        ctx = ToolContext(
            request_id=run_id,
            config=self.config,
            budget=budget,
            share_private=share_private,
            on_token=(emit_token if stream else None),
            stream=stream,
            owner=owner,
        )
        # Tool-facing event emitter (e.g. deliver.files surfacing a download).
        # Reuses the loop's emit so events get trace + seq + the live sink.
        async def tool_emit(etype: str, data: dict) -> None:
            await emit(etype, budget.iterations, data)
        ctx.emit = tool_emit

        # ---- Sub-agent seam: ctx.spawn(...) runs a nested, bounded agent ----
        a_cfg = self.config.get("agent", {}) or {}
        max_depth = int(a_cfg.get("max_depth", 2))
        budget_obj = budget                 # outer Budget (closure param shadows name)
        share_private_outer = share_private

        async def spawn(task: str, *, tools: list[str] | None = None,
                        model: str | None = None, name: str | None = None,
                        budget: dict | None = None,
                        share_private: bool | None = None) -> dict:
            if depth + 1 > max_depth:
                return {"status": "error", "answer": "",
                        "error": f"max sub-agent depth ({max_depth}) reached; "
                                 "a sub-agent cannot spawn deeper here"}
            # Allowlist can only ever NARROW what the parent had — never escalate.
            child_tools = tools
            if allowed is not None:
                if child_tools is None:
                    child_tools = list(allowed)
                else:
                    child_tools = [t for t in child_tools if t in set(allowed)]
            # Carve a sub-budget clamped to the parent's REMAINING allowance.
            pb = budget_obj
            req = budget or {}
            rem_cost = max(0.0, pb.max_cost_usd - pb.cost_usd)
            rem_tok = max(0, pb.max_total_tokens - pb.total_tokens)
            rem_wall = max(1.0, pb.max_wall_clock_s - pb.elapsed_s)
            child_overrides = {
                "max_cost_usd": min(float(req.get("max_cost_usd", rem_cost)), rem_cost),
                "max_total_tokens": min(int(req.get("max_total_tokens", rem_tok)), rem_tok),
                "max_iterations": int(req.get("max_iterations",
                                              a_cfg.get("default_sub_iterations", 8))),
                "max_wall_clock_s": min(float(req.get("max_wall_clock_s", rem_wall)), rem_wall),
            }
            child_confirm = (_NestedConfirm(confirm_provider, emit, run_id)
                             if confirm_provider is not None else None)
            child_share = share_private if share_private is not None else share_private_outer
            await emit("subagent_start", budget_obj.iterations, {
                "name": name or "sub-agent", "depth": depth + 1,
                "model": model or self.model, "tools": child_tools,
                "task": task[:500],
            })
            child = await self.run(
                task, share_private=child_share, tools=child_tools,
                auto_confirm=auto_confirm, on_event=None,
                confirm_provider=child_confirm, model=model,
                depth=depth + 1, budget_overrides=child_overrides,
                owner=owner, think=think, stream=False,
            )
            # Reconcile the child's spend into the parent so the parent's ceilings
            # account for it (enforced on the parent's next tick).
            cs = child.get("budget", {})
            ct = cs.get("tokens", {})
            budget_obj.cost_usd += cs.get("cost_usd", 0.0)
            budget_obj.tokens_prompt += ct.get("prompt", 0)
            budget_obj.tokens_completion += ct.get("completion", 0)
            budget_obj.tokens_cached += ct.get("cached", 0)
            await emit("subagent_finish", budget_obj.iterations, {
                "name": name or "sub-agent", "depth": depth + 1,
                "status": child.get("status"), "sub_run_id": child.get("run_id"),
                "budget": cs,
            })
            return child

        ctx.spawn = spawn

        final_answer = ""
        status = "ok"
        error_msg = ""
        budget_warned = False

        try:
            while True:
                budget.tick()
                # Once the run nears any ceiling, nudge the model to land the
                # plane: save progress, leave a resume note, summarize, and stop —
                # instead of getting hard-cut mid-edit with nothing usable.
                if not budget_warned and warn_fraction:
                    pr, dim = budget.pressure()
                    if pr >= warn_fraction:
                        budget_warned = True
                        messages.append({"role": "system", "content": _budget_warning(pr, dim)})
                        await emit("budget_warning", budget.iterations,
                                   {"pressure": round(pr, 2), "dimension": dim})
                # ---- Model turn (streaming if a UI wants live tokens) ----
                if stream:
                    turn = await self._model_turn_streaming(
                        messages, tools_schema,
                        lambda t, scope="brain": emit_token(t, scope, eff_model),
                        model=eff_model, think=think)
                else:
                    turn = await self._model_turn(messages, tools_schema,
                                                  model=eff_model, think=think)
                # Strip any <think>…</think> from the answer text before it reaches
                # the user, history, or the trace. (Streaming already routes think
                # to the "reasoning" scope and keeps content clean; this also covers
                # the non-streaming CLI path, where content arrives whole.)
                _m = turn["message"]
                if _m.get("content"):
                    _m["content"] = _strip_think(_m["content"]) or None
                await emit("model_turn", budget.iterations, {
                    "model": eff_model,
                    "usage": turn.get("usage", {}),
                    "tool_calls": [
                        {"name": tc["function"]["name"], "args": tc["function"]["arguments"]}
                        for tc in (turn["message"].get("tool_calls") or [])
                    ],
                    "content": turn["message"].get("content") or "",
                    "content_len": len(turn["message"].get("content") or ""),
                })

                usage = turn.get("usage", {})
                _cost_before = budget.cost_usd
                budget.add_usage(
                    eff_model,
                    prompt=usage.get("prompt_tokens", 0),
                    completion=usage.get("completion_tokens", 0),
                    cached=usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                            if isinstance(usage.get("prompt_tokens_details"), dict) else 0,
                    cost_table=self.cost_table,
                )
                await emit_cost(eff_model, budget.cost_usd - _cost_before)

                msg = turn["message"]
                messages.append(msg)
                tool_calls = msg.get("tool_calls") or []

                # ---- Termination: no tool calls = final answer ----
                if not tool_calls:
                    final_answer = msg.get("content") or ""
                    break

                # ---- Execute tools ----
                for tc in tool_calls:
                    name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"]
                    if allowed is not None and name not in allowed:
                        # The selected allowlist is a hard boundary, not just an
                        # exposure hint. Matters most for sub-agents — a research
                        # child literally cannot execute fs.write even if it tries.
                        args = None
                        result = ToolResult(
                            status="error", result=None, tool_name=name,
                            error=f"tool '{name}' is not permitted in this run")
                    else:
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except json.JSONDecodeError as e:
                            args = None
                            result = ToolResult(status="error", result=None, tool_name=name,
                                                error=f"invalid JSON args: {e}")
                        else:
                            # Loop guard
                            call_sig = self._call_signature(name, args)
                            if recent_calls.count(call_sig) >= 2:
                                result = ToolResult(
                                    status="error", result=None, tool_name=name,
                                    error="duplicate tool call detected (loop guard); "
                                          "vary the arguments or stop calling this tool",
                                )
                            else:
                                recent_calls.append(call_sig)
                                if len(recent_calls) > 20:
                                    recent_calls.pop(0)
                                # Privacy gate
                                self._enforce_privacy(name, args, messages, private_taint, share_private)
                                # Confirmation gate: pause for human approval on tools
                                # that declare requires_confirmation (e.g. job.start,
                                # git.commit). No-op unless confirmation.enabled.
                                tool_obj = self.registry.get(name)
                                # Gate on the tool's own requires_confirmation flag,
                                # OR because the call reaches a cloud LLM (it's in
                                # privacy.remote_llm_tools) and confirm_cloud_calls is
                                # on — sending data off-box and spending money deserves
                                # the same approval pause as a write/commit.
                                confirm_cloud = (self.config.get("confirmation", {}) or {}
                                                 ).get("confirm_cloud_calls", True)
                                needs_confirm = (
                                    (tool_obj is not None and tool_obj.requires_confirmation)
                                    or (confirm_cloud and self._is_cloud_tool(name)))
                                if (needs_confirm
                                        and not await self._confirm(name, args, run_id,
                                                                    auto_confirm, emit,
                                                                    confirm_provider)):
                                    result = ToolResult(
                                        status="error", result=None, tool_name=name,
                                        error="declined: human did not approve this "
                                              "tool call",
                                    )
                                else:
                                    result = await self._execute_tool(name, args, ctx)

                    await emit("tool_result", budget.iterations, {
                        "tool": name,
                        "args": args,
                        "status": result.status,
                        "error": result.error,
                        "result_preview": (result.to_model_message()[:1500]
                                           if result.status == "ok" else None),
                        "latency_ms": result.latency_ms,
                        "tokens": result.tokens_used,
                        "private": result.private,
                    })
                    trajectory.append(_traj_entry(name, args, result))

                    # Update budget with tool's own LLM usage (call_claude etc)
                    if result.tokens_used:
                        _tc_before = budget.cost_usd
                        budget.add_usage(
                            result.tokens_used.get("model", name),
                            prompt=result.tokens_used.get("prompt", 0),
                            completion=result.tokens_used.get("completion", 0),
                            cached=result.tokens_used.get("cached", 0),
                            cost_table=self.cost_table,
                        )
                        await emit_cost(result.tokens_used.get("model", name),
                                        budget.cost_usd - _tc_before)

                    # Append result to conversation
                    msg_idx = len(messages)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": name,
                        "content": result.to_model_message(),
                    })
                    if result.private:
                        private_taint.add(msg_idx)

        except asyncio.CancelledError:
            # Cancelled via the web /cancel endpoint (or task cancellation). Note:
            # detached job.* processes keep running by design — only the agent
            # loop stops. Swallow to produce a clean finish + final event.
            status = "cancelled"
            error_msg = "run cancelled"
            final_answer = f"[Run cancelled]\nWork so far: {final_answer or '(none)'}"
            log.info("Run %s cancelled", run_id)
        except BudgetExceeded as e:
            status = "budget_exceeded"
            error_msg = f"{e.reason}: {e.details}"
            log.warning("Budget exceeded: %s", error_msg)
            final_answer = (
                f"[Run terminated: {e.reason}]\n"
                f"Partial result based on work so far: {final_answer or '(no answer produced yet)'}"
            )
        except PrivacyViolation as e:
            status = "error"
            error_msg = f"privacy_violation: {e}"
            log.warning("Privacy violation: %s", e)
            final_answer = f"[Run terminated: privacy violation. {e}]"
        except Exception as e:
            status = "error"
            error_msg = f"{type(e).__name__}: {e}"
            log.exception("Unexpected error in agent loop")
            final_answer = f"[Internal error: {error_msg}]"

        summary = budget.summary()
        traj_str = _format_trajectory(trajectory)
        self.trace.finish_run(run_id, status, final_answer, error_msg, summary)
        await emit("run_finish", budget.iterations, {
            "status": status, "answer": final_answer,
            "error": error_msg or None, "budget": summary,
            "trajectory": traj_str,
        })

        return {
            "run_id": run_id,
            "status": status,
            "answer": final_answer,
            "error": error_msg or None,
            "budget": summary,
            "trajectory": traj_str,
        }

    # ---------- Internal helpers ----------

    async def _confirm(self, name: str, args: dict, run_id: str,
                       auto_confirm: bool, emit, confirm_provider=None) -> bool:
        """Ask a human to approve a confirmation-required tool call.

        Whether to ask is driven by the `confirmation` block in runtime.yaml:
          enabled: true|false        # master switch (default: true)
          non_interactive: allow|deny  # no-TTY fallback for the built-in prompt
        A per-run `auto_confirm=True` bypasses the prompt entirely.

        HOW to ask is delegated to `confirm_provider` when given (e.g. the web
        UI's provider emits a confirmation_request and waits for /approve). With
        no provider, the built-in TTY prompt / non_interactive fallback is used,
        preserving the CLI behaviour.
        """
        import sys
        cfg = self.config.get("confirmation", {}) or {}
        if not cfg.get("enabled", True):
            return True
        if auto_confirm:
            return True

        if confirm_provider is not None:
            approved = await confirm_provider.confirm(run_id, name, args, emit)
            decision_src = "provider"
        elif not sys.stdin.isatty():
            approved = cfg.get("non_interactive", "allow") == "allow"
            decision_src = f"non_interactive:{cfg.get('non_interactive', 'allow')}"
        else:
            preview = json.dumps(args, ensure_ascii=False)
            if len(preview) > 400:
                preview = preview[:400] + "…"
            prompt = (f"\n\033[33m[confirm]\033[0m {name}\n  args: {preview}\n"
                      f"  approve? [y/N] ")
            try:
                ans = await asyncio.get_event_loop().run_in_executor(None, input, prompt)
                approved = ans.strip().lower() in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                approved = False
        await emit("confirmation", 0, {"tool": name, "approved": approved, "via": decision_src})
        return approved

    async def _model_turn(self, messages: list[dict], tools_schema: list[dict],
                          model: str | None = None, think: bool = True) -> dict:
        """One call to the local orchestrator model via LiteLLM."""
        model = model or self.model
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{self.litellm_base}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "tools": tools_schema,
                    "tool_choice": "auto",
                    "temperature": 0.3,
                    # Qwen3 thinking switch. The tools template injects an empty
                    # <think></think> when this is false, so the brain skips
                    # chain-of-thought. Harmless for non-Qwen models (ignored).
                    "chat_template_kwargs": {"enable_thinking": think},
                },
                headers={"Authorization": "Bearer " + self._litellm_key()},
            )
            if r.status_code >= 400:
                # Surface the proxy's actual explanation instead of a bare code.
                body = r.text[:1000]
                log.error("model turn failed: HTTP %s from %s — %s",
                          r.status_code, model, body)
                raise RuntimeError(f"LiteLLM {r.status_code} for model "
                                   f"'{model}': {body}")
            data = r.json()
            return {
                "message": data["choices"][0]["message"],
                "usage": data.get("usage", {}),
            }

    async def _model_turn_streaming(self, messages: list[dict],
                                    tools_schema: list[dict], on_token,
                                    model: str | None = None,
                                    think: bool = True) -> dict:
        """Like _model_turn, but streams the response. Calls `await on_token(text)`
        for each content delta, assembles the streamed chunks back into the same
        {message, usage} shape the non-streaming path returns, and asks the proxy
        for usage via stream_options so cost still gets charged."""
        model = model or self.model
        body = {
            "model": model, "messages": messages, "tools": tools_schema,
            "tool_choice": "auto", "temperature": 0.3, "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": think},
        }
        content_parts: list[str] = []     # answer text only (think stripped)
        tool_calls: dict[int, dict] = {}   # index -> assembled tool call
        usage: dict = {}
        # Streaming <think> splitter state. `pend` holds a trailing fragment that
        # might be the start of a split tag; `in_think` tracks which side we're on.
        pend = ""
        in_think = False

        async def consume(text: str):
            nonlocal pend, in_think
            pend += text
            while pend:
                if not in_think:
                    idx = pend.find(_THINK_OPEN)
                    if idx == -1:
                        keep = _suffix_prefix_len(pend, _THINK_OPEN)
                        emit = pend[:len(pend) - keep]
                        if emit:
                            content_parts.append(emit)
                            if on_token:
                                await on_token(emit, "brain")
                        pend = pend[len(pend) - keep:]
                        return
                    if idx > 0:
                        seg = pend[:idx]
                        content_parts.append(seg)
                        if on_token:
                            await on_token(seg, "brain")
                    pend = pend[idx + len(_THINK_OPEN):]
                    in_think = True
                else:
                    idx = pend.find(_THINK_CLOSE)
                    if idx == -1:
                        keep = _suffix_prefix_len(pend, _THINK_CLOSE)
                        emit = pend[:len(pend) - keep]
                        if emit and on_token:
                            await on_token(emit, "reasoning")
                        pend = pend[len(pend) - keep:]
                        return
                    if idx > 0 and on_token:
                        await on_token(pend[:idx], "reasoning")
                    pend = pend[idx + len(_THINK_CLOSE):]
                    in_think = False

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{self.litellm_base}/v1/chat/completions", json=body,
                headers={"Authorization": "Bearer " + self._litellm_key()},
            ) as r:
                if r.status_code >= 400:
                    raw = await r.aread()
                    body_txt = raw.decode("utf-8", "replace")[:1000]
                    log.error("streaming model turn failed: HTTP %s — %s",
                              r.status_code, body_txt)
                    raise RuntimeError(f"LiteLLM {r.status_code} for model "
                                       f"'{model}': {body_txt}")
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if delta.get("content"):
                        await consume(delta["content"])
                    for tc in (delta.get("tool_calls") or []):
                        i = tc.get("index", 0)
                        slot = tool_calls.setdefault(i, {
                            "id": None, "type": "function",
                            "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]
        # Flush any held-back fragment (no further chunks to disambiguate it).
        if pend:
            if in_think:
                if on_token:
                    await on_token(pend, "reasoning")
            else:
                content_parts.append(pend)
                if on_token:
                    await on_token(pend, "brain")
        message: dict = {"role": "assistant",
                         "content": "".join(content_parts).strip() or None}
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        return {"message": message, "usage": usage}

    def _litellm_key(self) -> str:
        key = os.environ.get("LITELLM_MASTER_KEY")
        if not key:
            raise RuntimeError(
                "LITELLM_MASTER_KEY is not set. The proxy will reject the request "
                "(HTTP 400 auth). Source your env first:\n"
                "  set -a; source ~/.config/orchestrator.env; set +a\n"
                "or use the `orchenv` alias, then re-run."
            )
        return key

    async def _execute_tool(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        tool = self.registry.get(name)
        if tool is None:
            return ToolResult(status="error", result=None, tool_name=name,
                              error=f"unknown tool: {name}")
        start = time.monotonic()
        try:
            result = await tool.execute(args, ctx)
        except Exception as e:
            log.exception("Tool %s raised", name)
            return ToolResult(status="error", result=None, tool_name=name,
                              error=f"{type(e).__name__}: {e}",
                              latency_ms=int((time.monotonic() - start) * 1000))
        if not result.tool_name:
            result.tool_name = name
        if not result.latency_ms:
            result.latency_ms = int((time.monotonic() - start) * 1000)
        result.private = tool.private
        return result

    def _is_cloud_tool(self, name: str) -> bool:
        """True if this tool reaches a remote/cloud LLM (privacy.remote_llm_tools)."""
        return name in (self.config.get("privacy", {}).get("remote_llm_tools", []) or [])

    def _enforce_privacy(self, tool_name: str, args: dict, messages: list[dict],
                         private_taint: set[int], share_private: bool) -> None:
        """Block calling remote LLM tools when conversation contains private content."""
        if share_private:
            return
        if tool_name not in self.config["privacy"]["remote_llm_tools"]:
            return
        # Check if any tool argument value matches content from a tainted message.
        if not private_taint:
            return
        # Conservative check: if there's ANY tainted message in the conversation,
        # refuse the remote LLM call. (A finer-grained per-arg check is possible
        # but error-prone — better to be strict by default.)
        raise PrivacyViolation(
            f"cannot call {tool_name}: conversation contains private tool results "
            f"(from tainted messages). Re-run with --share-private to allow."
        )

    @staticmethod
    def _call_signature(name: str, args: dict) -> str:
        """Stable hash of a tool call for loop detection."""
        s = name + "|" + json.dumps(args, sort_keys=True, default=str)
        return hashlib.sha256(s.encode()).hexdigest()[:16]
