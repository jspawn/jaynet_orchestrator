"""Per-run config resolution for AgentRuntime.run (audit #1 follow-up).

All the "merge config section X with run_overrides, coerce the type, apply
the default" parsing that used to open run() lives here, so run() itself
reads as: resolve settings → build state → run the turn loop. Every field
keeps the rationale comment from its original inline site — these defaults
are load-bearing (most were born from a live failure or an eval autopsy),
so the WHY stays next to the value.

Nothing here imports from runtime.loop (import cycle) — the keyword
defaults moved IN here and loop.py imports them back.
"""
from __future__ import annotations

from dataclasses import dataclass

from .budget import Budget

# Just-reply bounce (agent.just_reply_check): compute/fresh-data markers in
# the request + a final answer with ZERO tool calls in the run → bounce once
# (live: just-replied "12000" for a computed 16000, multi-hop answers from
# memory). One-shot; a stated "no tool applies" clears it.
_DEFAULT_JUST_REPLY_KWS = (
    "how many", "how much", "count", "calculate", "compute", "average",
    "total of", "sum of", "percentage", "percent",
    "latest", "today", "this week", "this month", "this year",
    "price of", "weather", "news", "recent",
    "decode", "decrypt", "reversed", "most often", "the most", "highest",
    "lowest", "exact",
)

# Explicit accuracy demands in the user message seed a verification [must]
# (agent.exactness_gate): the requirements bounce then forces a verification
# pass before the final answer instead of a single-sample guess.
_DEFAULT_EXACTNESS_KWS = ("needs to be exact", "don't guess", "dont guess",
                          "do not guess", "be exact", "exactly right",
                          "count carefully", "double-check", "double check")

# Self-managed state file (agent.state_file, an adaptation of the CLM paper —
# Context Language Models, arxiv 2609.37725): the agent maintains state.md in
# its work_root with the fs.* tools it already has; the loop re-injects the
# file at the prompt tail every turn so it survives compaction. This is the
# built-in instruction overlay, injected once at run start when the feature is
# enabled; agent.state_file.instructions overrides it ("" = this default).
_DEFAULT_STATE_FILE_INSTRUCTIONS = (
    "state.md in your workspace is YOUR continuity memory. It is re-injected "
    "at the end of every turn and survives compaction — the transcript may "
    "not. Maintain it surgically: update it the moment a decision is made, a "
    "fact is established, or the plan changes. Keep it dense and current: "
    "goal + constraints, decisions with one-line reasons, exact "
    "paths/values/IDs, what's done, what's next. Delete what stops being "
    "true. Do not paste transcripts.")


def _anchor_choice(value) -> str:
    """Anchor enum coercion: YAML `off` parses to False, so normalize."""
    return "off" if value in (False, None, "off", "false", "") else str(value).lower()


def _int(value, default: int) -> int:
    """The loop_guard parse idiom: int(value or 0), default on type error."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _int_plain(value, default: int) -> int:
    """The agent/tool_selection parse idiom: int(value), default on error."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default: float) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


@dataclass
class RunSettings:
    """Resolved per-run configuration — the output of merging
    config/runtime.yaml with this run's run_overrides/budget_overrides."""

    eff_model: str                     # brain alias for THIS run
    run_overrides: dict                # the raw per-run overrides (_ro)
    budget_cfg: dict                   # merged budgets section (b_cfg)
    budget: Budget                     # ready-made spend ceiling object
    warn_fraction: float               # budget checkpoint nudge fraction
    # Per-run context behaviour overrides (the UI's Run options), layered
    # over config so the UI can flex them without a restart.
    compaction: dict
    # Each compaction pass that stubs a message breaks the prompt-cache
    # prefix at that point, so the pass runs only every `every` iterations
    # (compaction.every, default 1) — one re-prefill then amortizes several
    # stubs instead of one per turn.
    compaction_every: int
    # Complexity gate: brain rates each request 1-10 and escalates to the
    # `architect` tool at/above this threshold. Per-run override (quick
    # settings) wins over the config default; 0 disables the gate.
    architect_threshold: int
    # Sampler params apply to the BRAIN only. A sub-agent on a different
    # model keeps its own server-preset sampling — None for those runs
    # unless run_overrides["sampling_force"] opted in explicitly.
    sampling: dict | None
    parallel: dict                     # merged parallel_tools section
    lg_cfg: dict                       # raw loop_guard section
    # Loop-guard escalation: the duplicate-call guard refuses a repeated
    # call, but a stubborn model can re-emit it (or trivial variants)
    # forever. After this many guard refusals in one run, the next turn
    # runs with tools DISABLED to force the final answer. 0 = never force.
    guard_max: int
    # Near-duplicate guard: the exact guard misses the classic overthinking
    # pattern — the SAME search reworded ("price 2026 CHF" → "24h price CHF
    # 2026"). For query-like tools, calls whose arg-token Jaccard ≥ the
    # threshold count as duplicates too (2 similar allowed, 3rd blocked).
    # 0 disables. Distinct queries score low and pass freely; very short
    # same-host URLs can look alike (tokens <3 chars are dropped).
    near_dup_threshold: float
    near_dup_tools: set[str]
    # Repeat-error hard block: the failure-streak guard only NUDGES, and a
    # deterministic model can re-issue the same failing call forever (live:
    # gaia-e142056d ran code.check 26× into the dispatch-mode closed-tool
    # error — 2500s burned, every guard nudging, none stopping it). Once
    # the SAME (tool, args, error) has failed this many times, the next
    # identical attempt is refused at dispatch. 0 disables.
    hard_block_after: int
    # Stall hard-stop: the stall ladder (agent.stall_check) only NUDGES, and
    # a frozen brain can read/search/poll straight through every rung and
    # keep spinning (bakeoff lesson 9: 14+ tool calls over 47 minutes past
    # the final warning). Once the FINAL rung fires, StallLadderGuard arms
    # rs.stall_hard_stop and the pre-exec dispatch gate refuses every tool
    # call but the delegate/ask escape hatches until real progress disarms
    # it. false disables (nudges only).
    stall_hard_stop_on: bool
    # j-space badge+plan gate: the skill's protocol order is classify →
    # badge → plan → work — the badge (run.badge) AND a non-empty todos plan
    # must both be in place before file work OR delegation. While j-space is
    # loaded and the gate unlatched, fs.write/fs.edit outside .jspace/ and
    # delegate/spawn calls are REJECTED at dispatch with only the missing
    # opener(s) named. false = the one-shot nudge stays the only reminder.
    jspace_badge_gate_on: bool
    # Delegate gate: small MoE brains implement inline instead of delegating
    # (live eval: 17 inline edits, 0 delegations). Count successful inline
    # write/edit calls while delegation would route but stays unused; at the
    # threshold the tool result carries a directive, with delegate_enforce
    # inline edits are REJECTED from the threshold on. 0 disables.
    delegate_after: int
    delegate_enforce: bool
    # Soft→hard escalation (default on, brain gate only): a gated brain that
    # KEEPS writing inline after the soft directive gets write-like calls
    # REJECTED from twice the threshold on — the nudge is ignorable (live:
    # tb-regex-log wrote 4× past it), a rejection is not.
    delegate_escalate: bool
    # Stuck-delegate escalation: every distress hint that FIRES is recorded;
    # at loop_guard.stuck_delegate_after the run gets a concrete hand-over
    # directive naming the exact specialist.delegate call. One delegate call
    # disarms it.
    stuck_after: int
    # Auto-delegate (loop_guard.auto_delegate_after): the refusal gates TELL
    # the brain to delegate, but small brains retry the blocked call instead
    # (bakeoff blind-spot autopsy: 10+ explicit "call specialist.delegate"
    # rejections ignored, then emission collapse). After this many
    # delegate-pointing refusals the harness runs the delegation itself.
    # Once per run, brain depth only. 0 disables.
    auto_delegate_after: int
    agent_cfg: dict                    # raw agent section
    # Graceful iteration-cap exit (agent.final_synthesis, default on): a run
    # killed by max_iterations after gathering material gets ONE final
    # no-tools turn to summarize findings + name what's unverified, instead
    # of returning "[Run terminated]" (live: house-search child died at cap
    # seconds after finding the portal URLs it needed). The synthesis turn
    # runs after the budget tripped and is not charged against it.
    final_synthesis_on: bool
    max_depth: int                     # agent.max_depth — spawn nesting cap
    just_reply_check: bool
    just_reply_keywords: tuple
    # Fresh-perspective retry (GVS5H §4.4): re-delegating a task that
    # already FAILED inherits the brain's stuck framing — the reworded task
    # text anchors the child on the dead approach. When the SAME task
    # cluster comes back after `after` failures, the call is rewritten to
    # the RAW user request with a de-anchoring preamble. Depth-folded here.
    fresh_retry_enabled: bool
    fresh_retry_after: int
    # Stall ladder: count consecutive turns with NO mutation (reads,
    # searches and error results don't change anything). Every `after`
    # no-progress turns one rung fires, escalating act → dumbest-version/
    # delegate/ask → produce-or-ask. enabled=false / after=0 disables.
    stall_enabled: bool
    stall_after: int
    # Explicit accuracy demand in the user message ("this needs to be
    # exact", "don't guess"): seed a verification [must] harness-side so the
    # requirements bounce forces a verification pass before the final answer
    # (council-vote eval: one code.check in 65s answered a counting task).
    exactness_gate: bool
    exactness_keywords: tuple
    # tools.load seam: mid-run toolset expansion cap (each expansion
    # rebuilds the schema — one prompt-cache bust — so it's capped).
    max_expansions: int
    # Server-side cap on replayed chat history (orchestrator.
    # max_history_messages, 0 = unlimited): every replayed turn is re-sent
    # on every model turn of the run — a long chat otherwise grows each
    # run's cost unbounded.
    max_history: int
    # Working-anchor placement (off | system | trailing) + todos
    # re-injection when the anchor is off. Default off restores the plain
    # transcript — enable once the chat template accepts the placement.
    anchor_mode: str
    todos_reinject: str
    # Per-turn budget visibility (agent.anchor.budget, default on): the
    # brain never saw its iteration budget, so it over-verified trivial
    # answers and over-searched — a one-line used/limit readout rides at
    # the prompt tail every turn. false = zero injection.
    anchor_budget: bool
    # Self-managed state file (agent.state_file; CLM adaptation, arxiv
    # 2609.37725): the agent keeps state.md current via fs.* tools; the
    # loop re-reads it each turn and re-injects at the prompt tail so it
    # survives compaction. Default off (live A/B vs the harness summary).
    state_file_enabled: bool
    state_max_chars: int
    state_file_instructions: str
    # agent.max_bounces_per_answer (audit item 7): caps the bounce-nudges
    # ONE final answer may earn — each bounce costs a full model turn over
    # a growing context. Counted per answer; 0 disables.
    max_bounces: int
    # No-progress breaker: how many times the verifier may fail identically
    # (agent.verify.stall_after). NOTE: this key historically shadowed the
    # stall ladder's own `after` in run() — both default to 2, so
    # default-config behavior is unchanged.
    verify_stall_after: int
    # Context-pressure guard: one-shot nudge when a turn's prompt reaches
    # warn_fraction of the served window. run_overrides.context_tokens (the
    # /imp ctxguard) wins over config — an impersonated model usually has a
    # different served window. 0/unset disables.
    context_tokens: int

    def just_reply_armed(self, user_message, depth: int) -> bool:
        """Whether the just-reply bounce starts armed for this run."""
        return (self.just_reply_check and depth == 0
                and isinstance(user_message, str)
                and any(k in user_message.lower()
                        for k in self.just_reply_keywords))


def parse_run_settings(config: dict, *, run_overrides: dict | None,
                       budget_overrides: dict | None, model: str | None,
                       default_model: str, depth: int) -> RunSettings:
    """Merge config + per-run overrides into one resolved settings object."""
    _ro = run_overrides or {}
    eff_model = model or default_model
    b_cfg = {**config["budgets"], **(budget_overrides or {})}
    budget = Budget(
        max_iterations=b_cfg["max_iterations"],
        max_wall_clock_s=b_cfg["max_wall_clock_s"],
        max_cost_usd=b_cfg["max_cost_usd"],
        max_total_tokens=b_cfg["max_total_tokens"],
        cached_token_weight=_float(b_cfg.get("cached_token_weight", 0.1), 0.1),
        wall_clock_grace_s=_float(b_cfg.get("wall_clock_grace_s", 0), 0.0),
        wall_clock_max_extensions=_int(b_cfg.get("wall_clock_max_extensions", 0), 0),
    )
    eff_compaction = {**(config.get("compaction") or {}),
                      **(_ro.get("compaction") or {})}
    eff_threshold = _ro.get("architect_threshold")
    if eff_threshold is None:
        eff_threshold = (config.get("architect") or {}).get("threshold", 0)
    eff_threshold = _int(eff_threshold, 0)
    # Sampler params apply to the BRAIN only. A sub-agent on a different
    # model (e.g. the specialist.delegate specialist) keeps its own
    # server-preset sampling — the brain's config defaults and per-run
    # overrides never touch the specialist. Exception:
    # run_overrides["sampling_force"] is the explicit opt-in for callers
    # that intentionally run a different model under pinned sampling (eval
    # benchmark variants) — chat quick-settings never set it, so the
    # impersonation invariant above stays intact.
    _ro_sampling = _ro.get("sampling") or {}
    if eff_model == default_model or (_ro_sampling and _ro.get("sampling_force")):
        eff_sampling = {**(config["orchestrator"].get("sampling") or {}),
                        **_ro_sampling}
        if eff_model == default_model:
            eff_sampling.setdefault("temperature", 0.7)  # brain fallback
    else:
        eff_sampling = None
    _pt_cfg = config.get("parallel_tools")
    _pt_base = _pt_cfg if isinstance(_pt_cfg, dict) else {"enabled": bool(_pt_cfg)}
    eff_parallel = {**_pt_base, **(_ro.get("parallel_tools") or {})}
    warn_fraction = _float(b_cfg.get("warn_fraction", 0.8) or 0, 0.0)
    compaction_every = _int(eff_compaction.get("every", 1) or 1, 1)

    _lg = config.get("loop_guard") or {}
    guard_max = _int(_lg.get("max_rejections", 6), 6)
    near_dup_threshold = _float(_lg.get("near_dup_threshold", 0.75) or 0, 0.75)
    near_dup_tools = set(_lg.get("near_dup_tools")
                         or ["web.search", "web.fetch", "arxiv.search"])
    hard_block_after = _int(_lg.get("hard_block_repeat_errors", 3), 3)
    stall_hard_stop_on = bool(_lg.get("stall_hard_stop", True))
    jspace_badge_gate_on = bool(_lg.get("jspace_badge_gate", True))
    delegate_after = _int(_lg.get("delegate_nudge_after", 3), 3)
    delegate_enforce = bool(_lg.get("delegate_enforce", False))
    delegate_escalate = bool(_lg.get("delegate_escalate", True))
    stuck_after = _int(_lg.get("stuck_delegate_after", 3), 3)
    auto_delegate_after = _int(_lg.get("auto_delegate_after", 2), 2)

    _ag = config.get("agent", {}) or {}
    final_synthesis_on = bool(_ag.get("final_synthesis", True))
    max_depth = _int_plain(_ag.get("max_depth", 2), 2)
    just_reply_check = bool(_ag.get("just_reply_check", True))
    just_reply_keywords = tuple(_ag.get("just_reply_keywords")
                                or _DEFAULT_JUST_REPLY_KWS)
    _fr = _ag.get("fresh_retry") or {}
    fresh_retry_enabled = bool(_fr.get("enabled", True)) and depth == 0
    fresh_retry_after = _int(_fr.get("after", 2), 2)
    _sc = _ag.get("stall_check") or {}
    stall_enabled = bool(_sc.get("enabled", True))
    stall_after = _int(_sc.get("after", 2), 2)
    exactness_gate = bool(_ag.get("exactness_gate", True))
    exactness_keywords = tuple(_ag.get("exactness_keywords")
                               or _DEFAULT_EXACTNESS_KWS)

    max_expansions = _int_plain((config.get("tool_selection") or {})
                                .get("max_expansions", 2), 2)
    max_history = _int((config.get("orchestrator") or {})
                       .get("max_history_messages") or 0, 0)
    _anchor = _ag.get("anchor", {}) or {}
    anchor_mode = _anchor_choice(_anchor.get("mode", "off"))
    todos_reinject = _anchor_choice(_anchor.get("todos_reinject", "trailing"))
    if todos_reinject not in ("trailing", "system", "off"):
        todos_reinject = "trailing"
    anchor_budget = bool(_anchor.get("budget", True))
    _sf = _ag.get("state_file", {}) or {}
    state_file_enabled = bool(_sf.get("enabled", False))
    state_max_chars = _int(_sf.get("max_chars", 8000) or 8000, 8000)
    state_file_instructions = str(_sf.get("instructions")
                                  or _DEFAULT_STATE_FILE_INSTRUCTIONS)
    max_bounces = _int(_ag.get("max_bounces_per_answer", 3), 3)
    verify_stall_after = _int_plain((_ag.get("verify", {}) or {})
                                    .get("stall_after", 2), 2)
    ctx_tokens = _int(_ro.get("context_tokens")
                      or (config.get("orchestrator") or {}).get("context_tokens")
                      or 0, 0)

    return RunSettings(
        eff_model=eff_model, run_overrides=_ro, budget_cfg=b_cfg, budget=budget,
        warn_fraction=warn_fraction, compaction=eff_compaction,
        compaction_every=compaction_every,
        architect_threshold=eff_threshold, sampling=eff_sampling,
        parallel=eff_parallel, lg_cfg=_lg, guard_max=guard_max,
        near_dup_threshold=near_dup_threshold, near_dup_tools=near_dup_tools,
        hard_block_after=hard_block_after,
        stall_hard_stop_on=stall_hard_stop_on,
        jspace_badge_gate_on=jspace_badge_gate_on,
        delegate_after=delegate_after, delegate_enforce=delegate_enforce,
        delegate_escalate=delegate_escalate, stuck_after=stuck_after,
        auto_delegate_after=auto_delegate_after, agent_cfg=_ag,
        final_synthesis_on=final_synthesis_on, max_depth=max_depth,
        just_reply_check=just_reply_check,
        just_reply_keywords=just_reply_keywords,
        fresh_retry_enabled=fresh_retry_enabled,
        fresh_retry_after=fresh_retry_after, stall_enabled=stall_enabled,
        stall_after=stall_after, exactness_gate=exactness_gate,
        exactness_keywords=exactness_keywords, max_expansions=max_expansions,
        max_history=max_history, context_tokens=ctx_tokens,
        anchor_mode=anchor_mode, todos_reinject=todos_reinject,
        anchor_budget=anchor_budget, state_file_enabled=state_file_enabled,
        state_max_chars=state_max_chars,
        state_file_instructions=state_file_instructions,
        max_bounces=max_bounces, verify_stall_after=verify_stall_after)
