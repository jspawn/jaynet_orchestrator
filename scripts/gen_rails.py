"""Regenerate docs/rails.md — the rail registry (doc audit 2026-10-05, #2).

One row per rail: name, phase, order, config key, default, one-line purpose,
and the live case it came from. Guard rows are read from the registries
(runtime/turn_guards.py PRE_TURN_GUARDS / POST_TOOL_GUARDS,
runtime/final_guards.py FINAL_ANSWER_GUARDS): name and ORDER come from the
lists, purpose and the live-case origin are harvested from the class
docstrings, and config key/default come from the curated META table below
(the keys are parsed from config in run_setup.py / guard __init__ code,
not introspectable). The dispatch-gate section (the pre-exec gate pipeline
in runtime/dispatch_guards.py DISPATCH_GATES, driven by AgentRuntime.run)
is fully curated, anchored by _LOOP_ANCHORS: every listed string must
appear in runtime/loop.py, runtime/run_setup.py or
runtime/dispatch_guards.py, so a renamed gate/key breaks the build instead
of going silently stale.

Usage (from the repo root):
  .venv/bin/python scripts/gen_rails.py           # regenerate docs/rails.md
  .venv/bin/python scripts/gen_rails.py --check   # fail (exit 1) when stale
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "docs" / "rails.md"

# Curated per-rail config key + default, keyed by rail name. A registry rail
# MISSING here is a hard error — adding a rail means documenting it.
META: dict[str, tuple[str, str]] = {
    # pre-turn (runtime/turn_guards.py PRE_TURN_GUARDS)
    "budget_warning": ("budgets.warn_fraction", "0.8"),
    "budget_final_notice": ("budgets.final_warn_fraction", "0.95"),
    "context_warning": ("orchestrator.context_tokens (+ budgets.warn_fraction)",
                        "262144 / 0.8"),
    "stall_check": ("agent.stall_check.enabled / .after", "true / 2"),
    "deliverable_warn": ("agent.deliverable_check.enabled / .warn_at",
                         "true / 0.75"),
    "wrap_up": ("loop_guard.max_rejections", "6"),
    # post-tool (POST_TOOL_GUARDS)
    "failure_streak": ("loop_guard.failure_nudge_after", "3"),
    "host_give_up": ("loop_guard.host_give_up_after", "4"),
    "verify_arm": ("— (bookkeeping for the final-answer bounces + fresh-retry)",
                   "—"),
    "delegate_nudge": ("loop_guard.delegate_nudge_after", "3"),
    "badge_watch": ("skill frontmatter requires_badge; gate: "
                    "loop_guard.jspace_badge_gate", "true"),
    # final-answer (runtime/final_guards.py FINAL_ANSWER_GUARDS)
    "cap": ("— (always on)", "—"),
    "trunc": ("— (always on)", "—"),
    "empty": ("orchestrator.reasoning_budget_tokens (signature input)", "0"),
    "markup": ("— (always on)", "—"),
    "requirements": ("— ([must] todos/requirements)", "—"),
    "deliverable": ("agent.deliverable_check.enabled", "true"),
    "verify_delegate": ("agent.verify_delegate_check", "true"),
    "just_reply": ("agent.just_reply_check", "true"),
    "procedure": ("agent.procedure_selector.enabled", "true"),
}

# The pre-exec tool-call gate pipeline (runtime/dispatch_guards.py
# DISPATCH_GATES, driven per call from AgentRuntime.run), in execution
# order (verified by reading dispatch_guards.py; the anchors below keep it
# honest).
# (name, config key, default, purpose, live origin)
DISPATCH_GATES: list[tuple[str, str, str, str, str]] = [
    ("malformed", "—", "—",
     "Unparseable tool-call entry becomes a recoverable error result, never "
     "a run-ending crash.", "—"),
    ("admin_only", "security.admin_only_tools", "list",
     "Role policy refused at dispatch, before the allowlist, so the model "
     "sees WHY.", "audit #1 (Sep 2026)"),
    ("allowlist", "run tools= selection", "—",
     "The selected tool allowlist is a hard boundary (sub-agents literally "
     "cannot execute outside it).", "—"),
    ("stall_hard_stop", "loop_guard.stall_hard_stop", "true",
     "Final stall-ladder rung fired and nothing mutated since: tool calls "
     "closed except delegate/ask/writes-with-a-fix.", "bakeoff: 14+ calls "
     "over 47 min past the final warning"),
    ("repeat_error_block", "loop_guard.hard_block_repeat_errors", "3",
     "This exact call already failed N times with the same error — refuse "
     "the deterministic re-run.", "gaia-e142056d"),
    ("jspace_badge_gate", "loop_guard.jspace_badge_gate", "true",
     "In a j-space run, writes and delegate/spawn calls are refused until "
     "badge + todos plan exist; latches open.", "dispatch-mode runs badged, "
     "then delegated with no plan"),
    ("strength_gate", "agent.strength_gate.enabled", "true",
     "A routed strength domain with a live/swappable holder: inline "
     "implementation is rejected until the first specialist.delegate.",
     "4/4 security delegates went out without strength="),
    ("dispatch_gate", "tools.code.brain_mode: dispatch", "verify (off)",
     "Dispatcher profile: source-file writes rejected from the FIRST call; "
     "the brain plans/delegates/verifies.", "—"),
    ("delegate_enforce", "loop_guard.delegate_enforce / .delegate_escalate",
     "false / true",
     "Hard mode of the delegate gate: the threshold inline write is "
     "rejected pre-exec — the rejection IS the message.", "tb-regex-log "
     "wrote 4× inline past the soft nudge"),
    ("json_args", "—", "—",
     "Invalid JSON args are replaced with {} in history (the broken string "
     "would 500 every later turn) plus an error result.",
     "tb-mcmc-sampling-stan died turn 3"),
    ("fresh_retry", "agent.fresh_retry.enabled / .after", "true / 2",
     "Same task cluster delegated again after N failures: the child gets "
     "the raw original request, de-anchored.", "—"),
    ("duplicate", "— (mutation-generation scoped, poll-safe exempt)", "2×",
     "The exact same call already ran twice with nothing it reads changed — "
     "the result would be identical.", "—"),
    ("near_duplicate", "loop_guard.near_dup_threshold / .near_dup_tools",
     "0.75",
     "Query-like tools: a third reworded-but-similar call is blocked with a "
     "synthesize-now error.", "—"),
    ("privacy", "confirmation.confirm_cloud_calls / privacy.share_private",
     "true / false",
     "A cloud-LLM call while the conversation holds private tool results "
     "needs an explicit human ok.", "—"),
    ("confirmation", "confirmation.enabled", "true",
     "State-changing / admin-grade / cloud calls ask the human before "
     "executing (non-admin sessions: auto_confirm forced off).",
     "audit #1 (Sep 2026)"),
]

# Strings that must appear in the loop modules — the anchor between the
# curated dispatch section and the code (a renamed key breaks the build).
# Config parsing moved to run_setup.py (audit #1 refactor), the gates to
# dispatch_guards.py, so all three files are searched.
_ANCHOR_FILES = ("runtime/loop.py", "runtime/run_setup.py",
                 "runtime/dispatch_guards.py")
_LOOP_ANCHORS = [
    "max_rejections", "hard_block_repeat_errors", "jspace_badge_gate",
    "delegate_enforce", "near_dup_threshold", "admin_only_tools",
    "fresh_retry", "confirm_cloud_calls", "brain_mode", "stall_hard_stop",
]

_LIVE_RE = re.compile(
    r"(?i)\b((?:seen )?live(?: eval)?\b\s*:?\s*[^.;)]{5,140})")
_BAKEOFF_RE = re.compile(r"\((bakeoff[^)]{5,140})\)", re.I)


def _first_sentence(doc: str) -> str:
    """The guard's one-line purpose: the first PARAGRAPH of its docstring
    (docstrings soft-wrap), cut at the first sentence end when long."""
    para = re.split(r"\n\s*\n", (doc or "").strip(), maxsplit=1)[0]
    text = re.sub(r"\s+", " ", para).strip()
    if not text:
        return "—"
    m = re.search(r"\.(?:\s|$)", text)
    if m and m.end() < 160:
        return text[:m.end()].strip()
    return (text[:110].rstrip() + "…") if len(text) > 110 else text


def _live_origin(doc: str) -> str:
    m = _LIVE_RE.search(doc or "") or _BAKEOFF_RE.search(doc or "")
    if not m:
        return "—"
    return re.sub(r"\s+", " ", (m.group(1) or "")).strip().rstrip(",")[:140]


def _esc(cell: str) -> str:
    return cell.replace("|", "\\|")


def _guard_rows(classes, phase: str) -> list[str]:
    rows = []
    for i, cls in enumerate(classes, 1):
        inst_name = getattr(cls, "name", "") or cls.__name__
        if inst_name not in META:
            raise SystemExit(
                f"gen_rails: rail '{inst_name}' ({cls.__name__}) has no META "
                "entry — add its config key + default to scripts/gen_rails.py")
        key, default = META[inst_name]
        doc = cls.__doc__ or ""
        rows.append(
            f"| {i} | `{inst_name}` | {phase} | `{_esc(key)}` | "
            f"{_esc(default)} | {_esc(_first_sentence(doc))} | "
            f"{_esc(_live_origin(doc))} |")
    return rows


def build() -> str:
    from runtime.final_guards import FINAL_ANSWER_GUARDS
    from runtime.turn_guards import POST_TOOL_GUARDS, PRE_TURN_GUARDS

    loop_src = "\n".join((ROOT / f).read_text(encoding="utf-8")
                         for f in _ANCHOR_FILES)
    missing = [a for a in _LOOP_ANCHORS if a not in loop_src]
    if missing:
        raise SystemExit(
            "gen_rails: loop anchors missing: " + ", ".join(missing)
            + " — the curated dispatch-gate section in scripts/gen_rails.py "
              "is stale; re-read runtime/dispatch_guards.py and update it")

    out = [
        "# Rail registry",
        "",
        "> Generated by `scripts/gen_rails.py` — re-run it after adding,",
        "> renaming or reordering a rail (CI fails on a stale page; the",
        "> curated sections live in the script). The execution sequence with",
        "> the diagram is [turn.md](turn.md). Registry order is load-bearing",
        "> everywhere below — it is the historical order of the inline era.",
        "",
        "## Pre-turn guards (runtime/turn_guards.py PRE_TURN_GUARDS)",
        "",
        "Fire at turn start, after the compaction pass, before the model call.",
        "",
        "| # | Rail | Phase | Config key | Default | Purpose | Live case |",
        "|---|---|---|---|---|---|---|",
        *_guard_rows(PRE_TURN_GUARDS, "pre_turn"),
        "",
        "## Tool-call gates (runtime/dispatch_guards.py — not registry guards)",
        "",
        "Sequential per-call rejections from the DISPATCH_GATES pipeline",
        "(extracted from run() in the audit #1 refactor, historical inline",
        "order kept). A rejected call is never executed; the model gets the",
        "error as its tool result.",
        "",
        "| # | Gate | Phase | Config key | Default | Purpose | Live case |",
        "|---|---|---|---|---|---|---|",
        *[f"| {i} | `{n}` | tool_call | `{_esc(k)}` | {_esc(d)} | "
          f"{_esc(p)} | {_esc(o)} |"
          for i, (n, k, d, p, o) in enumerate(DISPATCH_GATES, 1)],
        "",
        "## Post-tool guards (runtime/turn_guards.py POST_TOOL_GUARDS)",
        "",
        "Fire after each tool result is recorded; the hint text reassembles in",
        "the legacy fail → delegate → badge → host slot order regardless of",
        "side-effect order below.",
        "",
        "| # | Rail | Phase | Config key | Default | Purpose | Live case |",
        "|---|---|---|---|---|---|---|",
        *_guard_rows(POST_TOOL_GUARDS, "post_tool"),
        "",
        "## Final-answer guards (runtime/final_guards.py FINAL_ANSWER_GUARDS)",
        "",
        "Fire when the model returns text instead of tool calls; the first",
        "nudge applied restarts the turn. Bounded by the bounce cap",
        "(`agent.max_bounces_per_answer`, default 3, counted PER ANSWER) — at",
        "the cap the answer is accepted with a `bounce_cap` event.",
        "",
        "| # | Rail | Phase | Config key | Default | Purpose | Live case |",
        "|---|---|---|---|---|---|---|",
        *_guard_rows(FINAL_ANSWER_GUARDS, "final_answer"),
        "",
        "## Verifier (inline — deliberately not a guard)",
        "",
        "Runs only for `verify=` runs, after the final-answer guards accept",
        "the answer: the sandboxed check command must exit 0 with real tests,",
        "and protected test files are hash-compared against the pre-run",
        "baseline (tampering = fail). Bounded by `agent.verify.max_checks`",
        "(default 4) and `agent.verify.stall_after` (default 2); caller-",
        "declared `unprotect` paths are exempt with a visible waiver note.",
        "",
    ]
    return "\n".join(out)


def main():
    content = build()
    if "--check" in sys.argv[1:]:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != content:
            print("gen_rails: docs/rails.md is stale — regenerate with "
                  "scripts/gen_rails.py")
            raise SystemExit(1)
        print("gen_rails: docs/rails.md is current")
        return
    OUT.write_text(content, encoding="utf-8")
    n = content.count("\n| ") - 7  # table header rows
    print(f"docs/rails.md written ({n} rails)")


if __name__ == "__main__":
    main()
