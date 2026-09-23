"""Per-run mutable state for AgentRuntime.run (runtime/loop.py).

Code audit 2026-09-23 (P2 — turn AgentRuntime.run into a guard pipeline),
step 1: run() was one ~2,300-line method with ~250 locals and 14 nested
closures. The loop-carried mutable state moved here UNCHANGED — a pure move,
so behavior, event names, payload keys and ordering are exactly as before
(the fake-model regression suite proves it).

run() still assigns every field at the exact place the local used to be
initialized, so the rationale comments (audit references, live-case notes)
stay at those assignment sites in loop.py — look there for why a flag
exists. Deliberately NOT here: read-only config snapshots (thresholds,
keyword lists, keyword-derived bools) and per-turn locals (turn, msg,
plans, ...) stay locals in run(); immutable per-run plumbing (run_id,
emit, ctx) stays in closures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .budget import Budget
from .todos import TodoList


@dataclass
class RunState:
    """Loop-carried mutable state of one AgentRuntime.run call."""

    # Spend ceilings + accumulators; required, built from the budget config.
    budget: Budget
    # The growing transcript (system, replayed history, user, assistant,
    # tool results, nudges) — re-sent to the model every turn.
    messages: list[dict] = field(default_factory=list)
    # Frozen tool allowlist for the run (None = all tools).
    allowed: list[str] | None = None
    tools_schema: list[dict] = field(default_factory=list)
    expansions_used: int = 0
    # Lazily-started per-run subcall server (runtime/subcall.py); Any to
    # keep this module import-light (the import stays lazy in loop.py).
    subcall_server: Any = None
    # Privacy: indices of assistant/tool messages derived from private
    # tool results — cloud calls gate on this.
    private_taint: set[int] = field(default_factory=set)
    # Loop guards: recent exact call signatures and near-duplicate query
    # calls, each tagged with the mutation generation they ran in.
    recent_calls: list = field(default_factory=list)
    recent_query_calls: list = field(default_factory=list)
    mutation_gen: int = 0
    # Repeat-error hard block: (tool, args signature) → {error identity:
    # count} for this run's tool failures (loop_guard.hard_block_repeat_errors
    # — the dispatch gate refuses the next identical attempt past the cap).
    repeat_fails: dict = field(default_factory=dict)
    # Compact record of what the run did (folded into the answer) + the
    # exact structural list of invoked tools.
    trajectory: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    # Auto-loaded procedure for this run: its checkpoints feed the stall
    # ladder and the final-answer procedure check.
    proc_name: str | None = None
    proc_checkpoints: list[str] = field(default_factory=list)
    # Run outcome.
    final_answer: str = ""
    status: str = "ok"
    error_msg: str = ""
    # One-shot budget/context pressure nudges + live window fill.
    budget_warned: bool = False
    budget_final_warned: bool = False
    context_warned: bool = False
    last_prompt_tokens: int = 0
    # Loop-guard escalation: refusals so far + tools-off wrap-up turn state.
    guard_rejections: int = 0
    wrap_up: bool = False
    wrap_up_noted: bool = False
    # One-shot final-answer bounces live on the guard instances now
    # (runtime/final_guards.py, audit P2 step 2) — what remains here is
    # the state the loop itself reads: think_off_next (consumed by the
    # model-turn code each turn) and the bounce counter for
    # agent.max_bounces_per_answer (audit item 7), counted per answer and
    # reset by any turn with tool calls.
    think_off_next: bool = False
    answer_bounces: int = 0
    deliverable_warned: bool = False
    # Verify-the-delegate + just-reply guard inputs (the one-shot flags
    # themselves live on the guard instances now).
    delegate_turn: int = -1
    check_turn: int = -1
    just_reply_armed: bool = False
    any_tool_turn: int = -1
    # Crash/failure-loop escalation (consecutive same-signature failures)
    # + diminishing-returns-per-host tracking.
    fail_sig: str | None = None
    fail_count: int = 0
    host_fails: dict[str, int] = field(default_factory=dict)
    # Delegate gate + stuck-delegate escalation state.
    delegate_ok: bool = False
    inline_writes: int = 0
    delegated: bool = False
    stuck_signals: list[str] = field(default_factory=list)
    stuck_fired: bool = False
    web_calls: int = 0
    # Fresh-perspective retry: delegated task clusters + their outcomes.
    delegate_trials: list[dict] = field(default_factory=list)
    # Strength gate: (tag, alias, mode) once armed, else None.
    strength_gate: tuple[str, str, str] | None = None
    # Stall ladder: consecutive no-progress turns + next rung to fire.
    stall_turns: int = 0
    stall_rung: int = 0
    # Badge watch (skills with requires_badge frontmatter).
    badge_watch: str | None = None
    badged: bool = False
    badge_nudged: bool = False
    # Overthinking signal + first-turn window fill (run_finish payload).
    overthinking_markers: int = 0
    first_prompt_tokens: int = 0
    # Verifier gate state (attempts/passed/baseline) + no-progress breaker.
    verify_state: dict = field(default_factory=dict)
    verify_stall: dict = field(default_factory=dict)
    # Goal/progress anchor note (the agent keeps it current via note.set).
    progress: dict = field(default_factory=dict)
    # Harness todo list + last-emitted snapshots (no-change → no re-emit).
    todo_list: TodoList = field(default_factory=TodoList)
    _last_todos_emit: list = field(default_factory=list)
    _last_reqs_emit: list = field(default_factory=list)
    # Typed hand-off: files this run created/edited.
    files_touched: set[str] = field(default_factory=set)
    # Salience-aware compaction: message indices pinned via context.pin.
    pinned: set[int] = field(default_factory=set)
