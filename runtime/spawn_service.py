"""Sub-agent spawning (extracted from AgentRuntime.run(), audit #1 phase 2).

The run()-local `spawn` closure — child allowlist narrowing, sub-budget
carving against the parent's REMAINING allowance, nested confirm/ask
routing, the cloud spawn gate, child event forwarding and parent budget
reconciliation — as a per-run service. The loop builds one SpawnService and
hands `ctx.spawn = svc.spawn` to tools. The small public helpers it shares
with the slash-command path (_NestedConfirm/_NestedAsk/_child_progress_fwd)
and _child_budget live here; loop.py re-exports them for existing imports.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cloud_gate

log = logging.getLogger(__name__)


def _child_budget(req: dict | None, db: dict | None, default_sub_iterations: int,
                  rem_cost: float, rem_tok: int, rem_wall: float) -> dict:
    """Assemble a spawned sub-agent's budget.

    Precedence per dimension: the spawn call's own `req` (budget arg) > config
    `db` (agent.default_budget) > the parent's REMAINING allowance (cost/tokens/
    wall) or `default_sub_iterations` (iterations). Cost/tokens/wall are clamped to
    the parent's remaining, so a child can never out-spend its parent; iterations
    are per-run and not clamped against the parent's remaining iterations.
    A remaining allowance of 0 means the parent's dimension is DISABLED — the
    child then defaults to disabled too and any explicit cap is NOT clamped
    against it (Budget.check reads a 0 ceiling as "no ceiling"). The spawn call
    site refuses to spawn at all when an ENABLED parent dimension is already
    exhausted, so a 0 reaching here only ever means "disabled", never "spent".
    """
    req = req or {}
    db = db or {}
    it = req.get("max_iterations", db.get("max_iterations", default_sub_iterations))
    wall = float(req.get("max_wall_clock_s", db.get("max_wall_clock_s", rem_wall)))
    if rem_wall:
        wall = min(wall, rem_wall)
    cost = float(req.get("max_cost_usd", db.get("max_cost_usd", rem_cost)))
    if rem_cost:
        cost = min(cost, rem_cost)
    tok = int(req.get("max_total_tokens", db.get("max_total_tokens", rem_tok)))
    if rem_tok:
        tok = min(tok, rem_tok)
    return {
        "max_cost_usd": cost,
        "max_total_tokens": tok,
        "max_iterations": int(it),
        "max_wall_clock_s": wall,
    }


class _NestedConfirm:
    """Routes a sub-agent's confirmation request up to the parent run, so a
    child's confirmation-gated tool (e.g. fs.write) still prompts the human on
    the parent's live stream, against the parent's run_id."""

    def __init__(self, provider, parent_emit, parent_run_id: str):
        self._provider = provider
        self._emit = parent_emit
        self._run_id = parent_run_id

    async def confirm(self, run_id: str, name: str, args: dict, emit,
                      reason: str | None = None) -> bool:
        # Ignore the child's run_id/emit; use the parent's so the request and the
        # eventual /approve line up with what the UI is already listening to.
        return await self._provider.confirm(self._run_id, name, args, self._emit,
                                            reason=reason)


class _NestedAsk:
    """Routes a sub-agent's ask.user request up to the parent run, so a child's
    questions surface on the parent's live stream and resolve against the
    parent's run_id (the UI is only listening to the parent)."""

    def __init__(self, provider, parent_emit, parent_run_id: str):
        self._provider = provider
        self._emit = parent_emit
        self._run_id = parent_run_id

    async def ask(self, run_id: str, questions: list, emit):
        return await self._provider.ask(self._run_id, questions, self._emit)


def _child_progress_fwd(emit, on_todos=None, forward_todos=True):
    """Forward a spawned child's events to the parent's stream as compact
    progress lines (tool ✓/✗, commentary snippet, thinking, nested spawns).
    `emit` is an async (type, data) callable — the loop binds its own
    iteration, the slash path binds its run stream. A child's full-snapshot
    `todos` events are forwarded as-is when `forward_todos` (the ToDos panel
    shows the child's live progress); `on_todos`, when given, also syncs the
    parent's own TodoList state. The loop's spawn passes BOTH only for
    children meant to take over the parent's list (the architect's executor)
    — a plain sub-agent's internal list stays invisible so it can't silently
    replace the parent's plan (audit T3)."""
    async def _fwd(ev: dict) -> None:
        d = ev.get("data") or {}
        et = ev.get("type")
        if et == "tool_result":
            mark = "✓" if d.get("status") == "ok" else "✗"
            await emit("progress", {"label": f"↳ {d.get('tool', '?')} {mark}",
                                    "type": "tool",
                                    "ok": d.get("status") == "ok"})
        elif et == "model_turn":
            content = (d.get("content") or "").strip()
            if content:
                short = content[:150] + ("…" if len(content) > 150 else "")
                await emit("progress", {"label": f"↳ {short}", "type": "prose"})
        elif et == "model_start":
            await emit("progress", {"label": "↳ thinking…", "type": "thinking"})
        elif et == "subagent_start":
            await emit("progress", {"label": f"↳ spawn {d.get('name', 'sub-agent')}…",
                                    "type": "spawn"})
        elif et == "todos":
            if not forward_todos and on_todos is None:
                return                      # child's internal list: keep it invisible (audit T3)
            items = d.get("items") or []
            if on_todos is not None:
                try:
                    on_todos(items)
                except Exception:
                    log.exception("on_todos sync raised (continuing)")
            if forward_todos:
                await emit("todos", {"items": items})
        elif et == "progress":
            await emit("progress", d)           # bubble nested up
    return _fwd


@dataclass
class SpawnService:
    """Per-run ctx.spawn provider. Holds the run plumbing the former closure
    captured; `spawn` is the verbatim extraction."""

    runtime: Any                  # AgentRuntime
    rs: Any                       # RunState
    emit: Any
    depth: int
    max_depth: int
    brain_gate: bool
    agent_cfg: dict
    run_overrides: dict
    run_id: Any
    confirm_provider: Any
    ask_provider: Any
    share_private: bool           # the run's outer share_private
    auto_confirm: bool
    disabled_tools: Any
    work_root: Any
    extra_roots: Any
    run_tmp: Any
    project_id: Any
    owner: Any
    is_admin: bool
    think: bool

    async def spawn(self, task: str, *, tools: list[str] | None = None,
                    model: str | None = None, name: str | None = None,
                    budget: dict | None = None,
                    share_private: bool | None = None,
                    verify=None, todos_sync: bool = False,
                    work_root_path: str | None = None,
                    base_system: str | None = None,
                    sampling: dict | None = None) -> dict:
        # loop.py owns _BRAIN_GATED_CODE_TOOLS (brain-gate tool narrowing) —
        # imported lazily to keep this module import-order-independent.
        from runtime.loop import _BRAIN_GATED_CODE_TOOLS
        rs = self.rs
        emit = self.emit
        budget_obj = rs.budget
        if self.depth + 1 > self.max_depth:
            return {"status": "error", "answer": "",
                    "error": f"max sub-agent depth ({self.max_depth}) reached; "
                             "a sub-agent cannot spawn deeper here"}
        # Allowlist can only ever NARROW what the parent had — never escalate.
        # Exception: the brain gate narrows the BRAIN's direct toolset, not
        # the run's privileges — a delegate child implements with code.run
        # even though the brain itself can't call it (live demo: the gated
        # parent's allowlist stripped code.run from the specialist child,
        # which could write fib.py but not run it).
        child_tools = tools
        if rs.allowed is not None:
            child_allowed = set(rs.allowed)
            if self.brain_gate:
                child_allowed |= _BRAIN_GATED_CODE_TOOLS
            if child_tools is None:
                child_tools = list(rs.allowed)
            else:
                child_tools = [t for t in child_tools if t in child_allowed]
                if tools and not child_tools:
                    # An explicit request that intersects to NOTHING must not
                    # silently run with a broader (or auto-selected) toolset.
                    return {"status": "error", "answer": "",
                            "error": f"none of the requested tools {tools} are "
                                     f"permitted in this run — permitted: "
                                     f"{', '.join(sorted(rs.allowed))}"}
        # Carve a sub-budget clamped to the parent's REMAINING allowance.
        pb = budget_obj
        req = budget or {}
        rem_cost = max(0.0, pb.max_cost_usd - pb.cost_usd)
        rem_tok = max(0, pb.max_total_tokens - pb.total_tokens)
        # Wall 0 = disabled: the child inherits "no ceiling" (0) rather than a
        # bogus 1s clamp that would kill it on its second tick.
        raw_wall = (pb.max_wall_clock_s - pb.elapsed_s) if pb.max_wall_clock_s else 0.0
        # An ENABLED parent ceiling that is fully spent computes a remaining
        # allowance of 0 — and Budget.check reads a 0 ceiling as "no ceiling",
        # so carving now would hand the child an UNLIMITED budget. Refuse the
        # spawn instead. A DISABLED parent dimension (0) legitimately stays
        # unlimited below. Wall gets the same refusal as cost/tokens: the old
        # max(1.0, …) floor handed the child a ONE-SECOND ceiling that killed
        # it on its second tick — the stall hard-stop's auto-delegate died at
        # 5.6s with limit 1.0 after burning a 125s model swap to get there
        # (eval gaia-cca530fc, 2026-10-08).
        if pb.max_cost_usd and rem_cost <= 0:
            return {"status": "error", "answer": "",
                    "error": f"parent cost budget is exhausted "
                             f"(${pb.cost_usd:.4f} of ${pb.max_cost_usd:.4f} spent); "
                             f"a sub-agent would run with no cost ceiling — refused"}
        if pb.max_total_tokens and rem_tok <= 0:
            return {"status": "error", "answer": "",
                    "error": f"parent token budget is exhausted "
                             f"({pb.total_tokens} of {pb.max_total_tokens} spent); "
                             f"a sub-agent would run with no token ceiling — refused"}
        if pb.max_wall_clock_s and raw_wall <= 0:
            return {"status": "error", "answer": "",
                    "error": f"parent wall-clock budget is exhausted "
                             f"({pb.elapsed_s:.0f}s of {pb.max_wall_clock_s:.0f}s "
                             f"spent); a sub-agent would run with no time "
                             f"allowance — refused"}
        rem_wall = max(1.0, raw_wall) if pb.max_wall_clock_s else 0.0
        # Config defaults (agent.default_budget) fill in any dimension the spawn
        # call didn't set, with a per-run UI override (_ro.sub_budget) layered on
        # top of config; cost/tokens/wall then fall back to the parent's remaining
        # allowance, iterations to default_sub_iterations. Every dim is still capped
        # at the parent's remaining — a child can never out-spend its parent.
        db = {**(self.agent_cfg.get("default_budget") or {}),
              **(self.run_overrides.get("sub_budget") or {})}
        child_overrides = _child_budget(
            req, db,
            self.agent_cfg.get("default_sub_iterations", 16),
            rem_cost, rem_tok, rem_wall)
        child_confirm = (_NestedConfirm(self.confirm_provider, emit, self.run_id)
                         if self.confirm_provider is not None else None)
        child_ask = (_NestedAsk(self.ask_provider, emit, self.run_id)
                     if self.ask_provider is not None else None)
        child_share = (share_private if share_private is not None
                       else self.share_private)
        # Cloud gate (audit S1): a child on a cloud brain sends its WHOLE
        # conversation off-box, so the destination alias is gated exactly
        # like an llm.call — private-tainted run needs the privacy approval
        # (never auto-confirmed), otherwise confirm_cloud_calls decides.
        # Local aliases never gate. agent.spawn and chain `agent` steps both
        # funnel through here.
        gate = cloud_gate.spawn_gate(model, self.runtime.config,
                                     private_taint=bool(rs.private_taint),
                                     share_private=child_share)
        if gate:
            gate_args = {"task": task[:500], "model": model,
                         "name": name or "sub-agent"}
            if gate == "privacy":
                ok = await self.runtime._confirm_privacy(
                    "agent.spawn", gate_args, self.run_id, emit,
                    self.confirm_provider)
                if not ok:
                    return {"status": "error", "answer": "",
                            "error": f"blocked by privacy: the conversation contains "
                                     f"private tool results and spawning a sub-agent "
                                     f"on cloud model '{model}' was not approved. Use "
                                     f"a local model instead, or ask the user to "
                                     f"enable 'share with cloud' for this run."}
            else:
                ok = await self.runtime._confirm(
                    "agent.spawn", gate_args, self.run_id, self.auto_confirm,
                    emit, self.confirm_provider)
                if not ok:
                    return {"status": "error", "answer": "",
                            "error": f"declined: human did not approve spawning a "
                                     f"sub-agent on cloud model '{model}'"}
        await emit("subagent_start", budget_obj.iterations, {
            "name": name or "sub-agent", "depth": self.depth + 1,
            "model": model or self.runtime.model, "tools": child_tools,
            "task": task[:500],
        })
        # Surface a spawned agent's live steps in the parent's tool box:
        # forward each child event as a concise, typed progress line
        # (shared mapping with the slash path's spawn). A child's todos
        # snapshots forward/sync ONLY when the child is meant to take over
        # the parent's list (todos_sync=True — the architect's executor);
        # a plain sub-agent's internal list stays its own (audit T3).
        async def _child_emit(t, d):
            await emit(t, budget_obj.iterations, d)

        def _sync_child_todos(items):
            # Validated wholesale replace (caps + status vocabulary
            # enforced) — never write a child snapshot straight into the
            # parent state (defense-in-depth, audit T2).
            rs.todo_list.replace(items)
        _child_progress = _child_progress_fwd(
            _child_emit,
            on_todos=_sync_child_todos if todos_sync else None,
            forward_todos=todos_sync)
        # Optional per-child workspace override (e.g. specialist.delegate's
        # isolated worktree). Must resolve INSIDE this run's existing roots
        # — anything else would be a confinement escape from a model-chosen
        # path.
        _child_wr = self.work_root
        if work_root_path:
            cand = Path(work_root_path).resolve()
            _roots = [Path(r).resolve() for r in
                      ([self.work_root] if self.work_root else [])
                      + [str(r) for r in (self.extra_roots or [])]
                      + [str(self.run_tmp)]]
            if not any(cand == r or r in cand.parents for r in _roots):
                return {"status": "error", "answer": "",
                        "error": f"work_root_path {cand} is outside this "
                                 "run's allowed roots — refused"}
            _child_wr = str(cand)
        child = await self.runtime.run(
            task, share_private=child_share, tools=child_tools,
            disabled_tools=self.disabled_tools,
            auto_confirm=self.auto_confirm, on_event=_child_progress,
            confirm_provider=child_confirm, ask_provider=child_ask, model=model,
            depth=self.depth + 1, budget_overrides=child_overrides,
            # Children run STREAMED so their model turns are covered by the
            # stall watchdog — the non-streaming path has only the coarse
            # total turn timeout, so a hung child backend would otherwise
            # sit for up to turn_timeout_s. The child's token events are
            # simply ignored by the _child_progress handler above.
            owner=self.owner, work_root=_child_wr,
            extra_roots=self.extra_roots,
            project_id=self.project_id,
            # Role policy inherits: a non-admin run's children stay non-admin.
            is_admin=self.is_admin,
            think=self.think, stream=True,
            verify=verify,
            # Worker mode (agent.worker_prompt via specialist.delegate):
            # swap the child's base prompt from the full gate prompt to
            # the lean worker prompt — None keeps the gate prompt.
            base_system=base_system,
            # Per-role sampling (agent.role_temperature): pinned onto the
            # child even when it runs on a specialist alias — the whole
            # point is to override that preset's server-side defaults for
            # this kind of work (execution cold, ideation warm).
            run_overrides=({"sampling": dict(sampling), "sampling_force": True}
                           if sampling else None),
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
            "name": name or "sub-agent", "depth": self.depth + 1,
            "status": child.get("status"), "sub_run_id": child.get("run_id"),
            "budget": cs,
        })
        # The web /cancel cancels THIS task once per run. If it landed while
        # the child ran, the child's own CancelledError handler swallowed it
        # and returned a normal "cancelled" dict — the request is still
        # pending on this task, so re-raise or the parent would keep looping,
        # unaware it was cancelled. (Reconciliation above still ran.)
        cur = asyncio.current_task()
        if cur is not None and cur.cancelling() > 0:
            raise asyncio.CancelledError
        return child
