"""Agent reasoning loop.

A bounded Level-1 agent: model proposes tool calls, runtime executes them,
results fed back, repeats until the model produces a final answer or any
budget ceiling is hit.

Key responsibilities:
- Translate between OpenAI tool-call format and our ToolResult envelope
- Enforce privacy: block private tool results from being passed to remote LLMs
- Detect repeat tool calls (same name+args 3× with no intervening write) → loop guard
  (and after loop_guard.max_rejections refusals, a tools-off wrap-up turn forces the answer)
- Escalate crash-retry loops: N consecutive same-signature execution failures
  (loop_guard.failure_nudge_after) append a strategy-change hint to the tool
  result — execution tools report failures in their payload, invisible to the
  duplicate-call guards
- Update budget on every model turn and tool call
- Log every step to the trace DB

Model-call plumbing lives in runtime/model_client.py (ModelClientMixin) and the
verifier gate in runtime/verify.py (VerifyMixin) — both are composed into
AgentRuntime below; the private names they own are re-exported here so existing
imports (tests, scripts) keep working.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
from datetime import UTC
from pathlib import Path

import yaml

from runtime.env import env

from . import cloud_gate
from .budget import Budget, BudgetExceeded
from .final_guards import FINAL_ANSWER_GUARDS, GuardContext
from .model_client import (  # noqa: F401  (re-exported)
    _NULL_ASYNC_CTX,
    ModelClientMixin,
    ModelTurnStalled,
    _is_local_model,
    _sampler_body,
    _strip_think,
    _turn_body,
)
from .registry import ToolRegistry
from .run_state import RunState
from .selector import ToolSelector
from .skills import discover_skills_layered, render_catalog
from .todos import TodoList
from .tool_base import ToolContext, ToolResult
from .trace import Trace
from .turn_guards import (  # noqa: F401  (_exec_failure re-exported for tests)
    _DELEGATE_TOOLS,
    _POST_TOOL_HINT_SLOTS,
    POST_TOOL_GUARDS,
    PRE_TURN_GUARDS,
    ToolCallView,
    TurnGuardContext,
    _exec_failure,
    _gate_write_like,
)
from .verify import VerifyMixin, _verify_sig

log = logging.getLogger(__name__)

# Overthinking signal (arXiv 2606.00206): hesitation/branch markers in the
# brain's own assistant turns — the model reaching an answer then talking
# itself out of it. Counted per run, surfaced to the watchdog coroner.
_OVERTHINK_RE = re.compile(r"\b(?:wait|but|alternatively|hmm)\b", re.IGNORECASE)

# Deliverable check: absolute file paths (with extension) named in text.
# Lookbehind excludes URLs (http://...) and path fragments glued to a word;
# the char class excludes templates like incident_<IP>_<timestamp>.txt.
# The lookahead tolerates a trailing sentence period ("...write /app/out.txt.")
_DELIVERABLE_RE = re.compile(
    r"(?<![\w:/.-])/(?:[\w.-]+/)*[\w.-]+\.\w{1,10}(?![\w/-])")


def _strength_kw_hit(kw: str, msg: str) -> bool:
    """Strength-keyword match: word-boundary for short acronyms (<=4 chars,
    no space — "rce", "cve", "xss"), substring otherwise so stems like "vuln"
    still catch "vulnerable"/"vulnerability"."""
    if len(kw) <= 4 and " " not in kw:
        return bool(re.search(r"\b" + re.escape(kw) + r"\b", msg))
    return kw in msg

# Chat-template tool-call markup detection for the final-answer markup
# guard moved to runtime/final_guards.py (audit P2 step 2).

# Tools whose success means a file was created/edited — surfaced as files_changed.
_MUTATOR_TOOLS = {"fs.write", "fs.edit", "code.patch"}

# Default strength-domain keywords (routing nudge + strength gate). Config
# tool_selection.routing_nudge.strength_keywords overrides; short acronyms
# match on word boundaries via _strength_kw_hit ("rce" must not fire inside
# "source"), longer keywords stay substring so stems work.
_DEFAULT_STRENGTH_KEYWORDS = {
    "security": ["vulnerability", "vuln", "exploit", "pentest", "pen test",
                 "cve", "sql injection", "xss", "privilege escalation",
                 "malware", "forensic", "security audit", "rce",
                 "reverse shell", "intrusion", "incident response",
                 "security threat", "threat detection", "capture the flag"],
}

# Default procedure-shape keywords (agent.procedure_selector.shapes overrides).
# A shape tag in a skill's frontmatter marks it as a PROCEDURE — a distilled
# step-by-step for a task shape; a confident keyword match auto-loads it at
# run start. Deliberately narrow: a false positive injects a multi-thousand
# token body for the whole run.
_DEFAULT_PROCEDURE_SHAPES = {
    "implement-from-spec": ["implement the", "research paper",
                            "from the paper", "from scratch",
                            "passes the test", "write /app", "create /app",
                            "convert the"],
    "debug-and-fix": ["failing test", "tests fail", "test fails",
                      "fix the bug", "debug the", "broken build",
                      "fix the failing", "bug in the"],
    "research-and-verify": ["find the official", "official codebase",
                            "official repo", "official implementation",
                            "look up the", "find the source"],
}

# The stall ladder rung texts moved to runtime/turn_guards.py with the
# stall guard (audit P2 step 3).

# Tools that never produce a work product on their own: bookkeeping (todo
# list, pins, badges) and verify-only exec (code.check — no writes by
# contract). A turn of ONLY these does not reset the stall ladder — a
# hesitant brain can otherwise hide in planning or in re-running the same
# check forever (live: tb-huarong's 11 todos turns, then 14 code.check runs
# of the same analysis script). Legit verify loops interleave fs.edit/
# fs.write, which DO reset — only product-free streaks escalate.
_NO_PRODUCT_TOOLS = frozenset({"todos", "context.pin", "run.badge",
                               "code.check"})


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


def _traj_arg_hint(args: dict | None) -> str:
    """A short, non-sensitive hint of what a tool call was aimed at — taken from
    the call's *arguments* (the model's own inputs: a URL, query, path, model,
    collection), never from the result, so trajectory notes can't leak private
    tool output back into replayed history. `args` is None for calls rejected
    before parsing (allowlist / invalid-JSON gates) — hintless, not a crash.
    A parsed-but-non-dict payload (the model emitted a JSON list/string) is
    hintless too."""
    if not isinstance(args, dict):
        return ""
    for k in ("url", "query", "path", "task", "model", "collection", "name"):
        v = args.get(k)
        if v:
            s = str(v).replace("\n", " ").strip()
            return s[:70] + ("…" if len(s) > 70 else "")
    return ""


def _tc_function(tc) -> dict:
    """Best-effort access to a tool call's `function` payload. Models sometimes
    emit malformed tool-call entries (missing keys, non-dict values); treat
    anything unexpected as empty so the loop degrades to an error tool-result
    fed back to the model instead of dying as an internal error."""
    if isinstance(tc, dict) and isinstance(tc.get("function"), dict):
        return tc["function"]
    return {}


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




def _patch_tools_config(config: dict, patch: dict | None) -> dict:
    """Per-run overrides for the `tools:` config section (run_overrides
    "tools_patch": {<namespace>: {<key>: <value>}}). Returns a shallow copy
    with a deep-copied tools section — the shared runtime config is never
    mutated. Used by the eval harness to redirect persistent stores (memory,
    rag) at a per-case sandbox."""
    if not patch:
        return config
    import copy
    cfg = dict(config)
    tools = copy.deepcopy(config.get("tools") or {})
    for ns, kv in patch.items():
        if isinstance(kv, dict):
            merged = tools.get(ns) or {}
            merged.update(kv)
            tools[ns] = merged
    cfg["tools"] = tools
    return cfg


def _patch_run_config(config: dict, tools_patch: dict | None,
                      section_patch: dict | None) -> dict:
    """tools_patch plus per-run overrides for OTHER top-level sections
    (run_overrides "config_patch": {<section>: {<key>: <value>}}). Internal
    seam — the web layer never copies user input into run_overrides, so only
    server-side callers (the eval harness redirecting web.projects_dir at its
    sandbox) can set it. The shared runtime config is never mutated."""
    cfg = _patch_tools_config(config, tools_patch)
    if not section_patch:
        return cfg
    import copy
    cfg = dict(cfg)
    for section, kv in section_patch.items():
        if section == "tools" or not isinstance(kv, dict):
            continue                     # tools: goes through tools_patch
        merged = copy.deepcopy(config.get(section) or {})
        merged.update(kv)
        cfg[section] = merged
    return cfg


def _compact_messages(messages: list[dict], cfg: dict, pinned: set | None = None) -> int:
    """Shrink old, large tool-result messages in place to keep the re-sent
    transcript from ballooning every turn (the loop resends the whole list).

    A tool result, once a later model turn has consumed it, rarely needs to sit
    verbatim in context for the rest of the run — but it costs full tokens on
    every subsequent turn. We replace the body of large, older tool messages with
    a short stub that keeps the status + a head snippet and points at trace.query
    for the full text. Two kinds of message are protected from stubbing: the most
    recent `keep_last` tool messages (recency — the model is likely still working
    with them) AND any the agent has pinned via context.pin (salience — retention
    shouldn't be purely positional, or a rare-but-crucial early result gets stubbed
    while recent noise survives). Nothing else (system / user / assistant) is
    touched. We only mutate `content`, never the list length, so message indices
    (and the taint/pin sets keyed on them) stay valid. Idempotent.

    Returns the number of messages compacted (for telemetry). No-op unless
    cfg['enabled'] is true.
    """
    if not cfg or not cfg.get("enabled"):
        return 0
    max_chars = int(cfg.get("max_result_chars", 2000))
    keep_last = int(cfg.get("keep_last", 3))
    # Indices of tool messages, oldest→newest; protect the last `keep_last`
    # (recency) plus anything pinned (salience).
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    protect = set(tool_idx[-keep_last:]) if keep_last else set()
    if pinned:
        protect |= {i for i in pinned if 0 <= i < len(messages)}
    compacted = 0
    for i in tool_idx:
        if i in protect:
            continue
        m = messages[i]
        content = m.get("content") or ""
        if len(content) <= max_chars or '"__compacted__"' in content:
            continue
        head = content[:300].replace("\n", " ")
        # Preserve the ok/error signal so the model still reads the gist.
        status = "error" if '"status": "error"' in content[:60] else "ok"
        m["content"] = json.dumps({
            "status": status, "__compacted__": True, "head": head,
            "note": ("full result elided to save context; retrieve with "
                     "trace.query view=events run_id=<this run> if needed"),
        })
        compacted += 1
    # Image hygiene: image_url blocks (tool return_image payloads, user
    # attachments) cost image tokens on every re-sent turn. Keep only the most
    # recent image-bearing message intact; older blocks become a text marker.
    img_idx = [i for i, m in enumerate(messages)
               if isinstance(m.get("content"), list)
               and any(isinstance(b, dict) and b.get("type") == "image_url"
                       for b in m["content"])]
    for i in img_idx[:-1]:
        m = messages[i]
        m["content"] = [
            {"type": "text",
             "text": "[image elided to save context — re-capture if needed]"}
            if isinstance(b, dict) and b.get("type") == "image_url" else b
            for b in m["content"]]
        compacted += 1
    return compacted


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

def slash_spawn(runtime, *, run_id=None, owner=None, work_root=None,
                is_admin=True,
                confirm_provider=None, ask_provider=None, emit=None):
    """Build a ctx.spawn for contexts WITHOUT a parent agent run (slash commands).

    A slashed `/<tool>` executes in a bare ToolContext, so spawn-dependent tools
    (specialist.delegate, agent.spawn, architect, …) died with "sub-agents are not
    available". The returned callable runs the child as a depth-1 agent via
    runtime.run: config `agent.default_budget` caps it (the call's `budget` arg
    wins per dimension), confirmations/asks route to the caller's providers
    against its run_id, and child steps forward as progress lines. There is no
    parent budget to reconcile into — the config ceilings are the only clamp.
    is_admin carries the caller's role so a non-admin's spawned child keeps the
    admin-only tool boundary.
    """
    async def spawn(task: str, *, tools: list[str] | None = None,
                    model: str | None = None, name: str | None = None,
                    budget: dict | None = None,
                    share_private: bool | None = None, verify=None,
                    todos_sync: bool = False,
                    work_root_path: str | None = None,
                    base_system: str | None = None) -> dict:
        # todos_sync is accepted for signature parity with the loop's spawn;
        # the slash path has no parent list, so child todos events simply
        # forward to the stream (no state to sync). base_system likewise:
        # parity so a slashed specialist.delegate can run worker mode.
        a_cfg = runtime.config.get("agent", {}) or {}
        overrides = dict(a_cfg.get("default_budget") or {})
        overrides.setdefault("max_iterations",
                             int(a_cfg.get("default_sub_iterations", 8)))
        overrides.update(budget or {})

        async def _emit(t, d):
            if emit is not None:
                await emit(t, d)

        async def _nested_emit(t, _i, d):       # _NestedConfirm/Ask emit (t, i, d)
            await _emit(t, d)

        child_confirm = (_NestedConfirm(confirm_provider, _nested_emit, run_id)
                         if confirm_provider is not None else None)
        child_ask = (_NestedAsk(ask_provider, _nested_emit, run_id)
                     if ask_provider is not None else None)
        await _emit("subagent_start", {"name": name or "sub-agent", "depth": 1,
                                       "model": model or runtime.model,
                                       "tools": tools, "task": task[:500]})
        # Same confinement rule as the loop's spawn: a per-child workspace
        # override must stay inside the caller's roots.
        child_wr = work_root
        if work_root_path:
            cand = Path(work_root_path).resolve()
            roots = [Path(r).resolve() for r in ([work_root] if work_root else [])]
            if not any(cand == r or r in cand.parents for r in roots):
                return {"status": "error", "answer": "",
                        "error": f"work_root_path {cand} is outside the "
                                 "caller's roots — refused"}
            child_wr = str(cand)
        # Cloud gate (audit B9): the loop's ctx.spawn gates a cloud child brain
        # via cloud_gate.spawn_gate; this slash path called runtime.run
        # directly and skipped it. A slashed spawn on a cloud alias sends the
        # child's whole conversation off-box, so confirm_cloud_calls applies
        # here too (a slash run starts fresh — no taint, so only the standard
        # confirmation can trigger).
        gate = cloud_gate.spawn_gate(model, runtime.config,
                                     private_taint=False,
                                     share_private=bool(share_private))
        if gate:
            gate_args = {"task": task[:500], "model": model,
                         "name": name or "sub-agent"}
            ok = (confirm_provider is not None and
                  await confirm_provider.confirm(run_id, "agent.spawn",
                                                 gate_args, _emit))
            if not ok:
                return {"status": "error", "answer": "",
                        "error": f"declined: spawning a sub-agent on cloud model "
                                 f"'{model}' was not approved"}
        child = await runtime.run(
            task,
            share_private=bool(share_private),
            tools=tools,
            model=model,
            depth=1,
            budget_overrides=overrides,
            owner=owner,
            is_admin=is_admin,
            work_root=child_wr,
            confirm_provider=child_confirm,
            ask_provider=child_ask,
            on_event=_child_progress_fwd(_emit) if emit is not None else None,
            # Streamed so the stall watchdog covers the child's model turns —
            # same reasoning as the loop's own spawn.
            stream=True,
            verify=verify,
            base_system=base_system,
        )
        await _emit("subagent_finish", {"name": name or "sub-agent", "depth": 1,
                                        "status": child.get("status"),
                                        "sub_run_id": child.get("run_id"),
                                        "budget": child.get("budget", {})})
        return child

    return spawn


# Brain-only coding-tool gate (tools.code.brain_mode: verify). When a coding
# specialist is present, the brain loses the write/run coding tools and gets
# code.check instead, so implementation MUST route through specialist.delegate.
# The recurring eval failure was the brain grinding code.run inline and never
# delegating (gaia-50ad0280: 15 calls, gaia-65afbc8a: 36, tb-regex-log: 28);
# prompt tripwires were ignorable, a missing tool is not.
_BRAIN_GATED_CODE_TOOLS = frozenset({"code.run", "code.execute", "code.patch"})

# _DELEGATE_TOOLS / _CHECK_TOOLS live in runtime/turn_guards.py with the
# post-tool guards (audit P2 step 3); _DELEGATE_TOOLS is imported above
# (the inline pre-exec gates still use it).

# Compute/fresh-data markers in the user message arm the just-reply bounce
# (agent.just_reply_check): a final answer delivered with ZERO tool calls in
# the whole run bounces once. The trigger fires ONLY on tool-less runs, so
# keyword overreach costs at most one clarifying turn on knowledge questions
# ("how many legs has a dog" → "no tool applies" clears it) — while the live
# failure cluster it exists for (just-replied counts, decoded strings,
# multi-hop answers from memory) is exactly "question + zero tools".
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


def _coding_specialist_present(config: dict) -> bool:
    """A specialist slot whose preset carries the 'coding' strength tag."""
    models = config.get("models") or {}
    presets = models.get("presets") or {}
    slots = models.get("slots") or {}
    for slot in ("specialist", "specialist2", "specialist3"):
        p = presets.get(slots.get(slot) or "") or {}
        if "coding" in (p.get("strengths") or []):
            return True
    return False


def _brain_gate_active(config: dict, depth: int) -> bool:
    if depth != 0:
        return False
    code_cfg = (config.get("tools") or {}).get("code") or {}
    return (str(code_cfg.get("brain_mode") or "full") in ("verify", "dispatch")
            and _coding_specialist_present(config))


def _tool_policy_match(name: str, patterns) -> bool:
    """Match a tool name against role-policy entries (security.admin_only_tools):
    exact, or a trailing-* prefix (`job.*` matches job.start, job.status, …)."""
    for p in patterns:
        p = str(p).strip()
        if not p:
            continue
        if p.endswith("*"):
            if name.startswith(p[:-1]):
                return True
        elif name == p:
            return True
    return False


def _brain_dispatch_active(config: dict, depth: int) -> bool:
    """dispatch mode = verify PLUS the hard dispatcher profile: the brain's
    own fs.write/fs.edit calls into source files are rejected pre-exec (no
    threshold) — it plans, delegates and verifies, it never authors code.
    The field's consensus fix for 'orchestrator does the work itself'
    (hermes kanban-orchestrator, icdev dispatcher mode): don't persuade,
    remove the capability."""
    if depth != 0:
        return False
    code_cfg = (config.get("tools") or {}).get("code") or {}
    return (str(code_cfg.get("brain_mode") or "full") == "dispatch"
            and _coding_specialist_present(config))


# Source-file targets the dispatch gate rejects (fs.write/fs.edit). Prose,
# config and data files stay writable — the brain still takes notes, writes
# reports and edits its own configs. Extension match plus the well-known
# extension-less build files.
_CODE_FILE_EXTS = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue",
    ".svelte", ".rs", ".go", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
    ".java", ".kt", ".kts", ".scala", ".rb", ".php", ".cs", ".fs", ".fsx",
    ".vb", ".swift", ".m", ".mm", ".lua", ".pl", ".pm", ".r", ".jl", ".ex",
    ".exs", ".erl", ".hrl", ".hs", ".ml", ".mli", ".sh", ".bash", ".zsh",
    ".ps1", ".bat", ".cmd", ".sql", ".html", ".htm", ".css", ".scss",
    ".less",
})
_CODE_FILE_NAMES = frozenset({
    "dockerfile", "makefile", "cmakelists.txt", "jenkinsfile", "rakefile",
    "gemfile", "vagrantfile", "brewfile",
})


def _code_file_target(args) -> bool:
    """True when fs.write/fs.edit args target a source-code path."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return False
    if not isinstance(args, dict):
        return False
    path = str(args.get("path") or "").strip().lower()
    if not path:
        return False
    base = path.rsplit("/", 1)[-1]
    if base in _CODE_FILE_NAMES:
        return True
    dot = base.rfind(".")
    return dot > 0 and base[dot:] in _CODE_FILE_EXTS


# Gate-aware descriptions (brain_mode: verify/dispatch): the routing rule is
# appended to the description the gated brain reads at the DECISION point — a
# standing prompt bullet is 30k tokens behind it by the time it picks fs.write
# for an implementation (live: tb-regex-log, 6 inline writes past the soft
# nudge). Dispatch wording states the hard rejection, not advice.
_BRAIN_GATE_DESC = {
    "fs.write": " Config, notes, prose and data files only — never author "
                "code inline: implementations go to specialist.delegate, you "
                "verify their result with code.check.",
    "fs.edit": " Prose/config edits only, never inline code — "
               "implementations go to specialist.delegate.",
    "code.check": " Run checks only (tests, linters, builds, small "
                  "computations) — never write or install files through its "
                  "command.",
}
_BRAIN_DISPATCH_DESC = {
    "fs.write": " Deliverables, notes, configs and data files (txt, md, "
                "json, yaml, csv, log, ...) are ALWAYS writable — write them "
                "freely. Only SOURCE-CODE files (py, js, ts, rs, go, sh, "
                "Dockerfile, ...) are REJECTED by the harness: code "
                "implementations go to specialist.delegate, you verify their "
                "result with code.check.",
    "fs.edit": " Prose, config and data-file edits are always fine. Only "
               "SOURCE-CODE edits are REJECTED by the harness — "
               "implementations go to specialist.delegate.",
    "code.check": " Run checks only (tests, linters, builds, small "
                  "computations) — never write or install files through its "
                  "command.",
}


def _brain_gate_schema_notes(tools_schema: list[dict],
                             dispatch: bool = False) -> list[dict]:
    """Append gate wording to the matching tool schemas (copies only —
    the registry's canonical descriptions stay untouched)."""
    notes = _BRAIN_DISPATCH_DESC if dispatch else _BRAIN_GATE_DESC
    out = []
    for s in tools_schema:
        fn = dict(s.get("function") or {})
        note = notes.get(fn.get("name"))
        if note:
            fn["description"] = (fn.get("description") or "") + note
            s = {**s, "function": fn}
        out.append(s)
    return out


def _brain_code_gate(config: dict, registry, allowed: list[str] | None,
                     depth: int, disabled: set[str] | None) -> list[str] | None:
    """Swap the brain's coding tools for code.check under brain_mode: verify.
    Sub-agents (depth>0), full mode, and no-coding-specialist installs pass
    through untouched; force_tools/goal appends later may re-add on top."""
    if not _brain_gate_active(config, depth):
        return allowed
    disabled = disabled or frozenset()
    known = {t.name for t in registry.all()}
    out = list(allowed) if allowed is not None else sorted(known)
    out = [n for n in out if n not in _BRAIN_GATED_CODE_TOOLS]
    if "code.check" in known and "code.check" not in disabled \
            and "code.check" not in out:
        out.append("code.check")
    return out


class AgentRuntime(ModelClientMixin, VerifyMixin):
    def __init__(self, config_path: str | Path | None = None,
                 config_overrides: dict | None = None):
        from runtime.paths import CONFIG, CUSTOM_SKILLS_DIR, CUSTOM_TOOLS_DIR
        self.config_path = Path(config_path) if config_path else CONFIG
        with self.config_path.open() as f:
            self.config = yaml.safe_load(f)
        # Admin-persisted config overrides (the web layer's UserStore) merge
        # BEFORE registry/plugin discovery below — a plugin toggled in Admin
        # must be visible to plugins.load at boot. The CLI passes nothing.
        for _dp, _val in (config_overrides or {}).items():
            _parts = str(_dp).split(".")
            _d = self.config
            for _p in _parts[:-1]:
                _d = _d.setdefault(_p, {})
            _d[_parts[-1]] = _val
        from runtime.config_check import warn_unknown_sections
        warn_unknown_sections(self.config, log)

        orch_root = self.config_path.parent.parent
        tools_root = orch_root / "tools"
        self.registry = ToolRegistry(tools_root)
        self.registry.discover()
        # Custom layer (ORCH_DATA/custom): admin-created Python tools and
        # declarative API connectors. Both refuse names already registered.
        if CUSTOM_TOOLS_DIR.is_dir():
            self.registry.discover_extra(CUSTOM_TOOLS_DIR)
        # Connectors (declarative YAML → tools): legacy single files and
        # multi-tool packages alike load through the package registry
        # (runtime/connectors.py) — enabled/RO-RW/settings state applied,
        # hot-swappable from the admin Connectors tab without a restart.
        from runtime import connectors as _connectors
        _connectors.refresh(self.registry)
        # Plugins (runtime/plugins.py): enabled+available bundles register
        # their tools and hooks here. Disabled/missing-dep plugins are never
        # imported. Status list is kept for the web layer (admin Plugins tab,
        # plugin routes, plugin skill layers).
        from runtime import plugins as plugin_loader
        # plugin_handles: live-registration bookkeeping per enabled plugin
        # (tools/hooks/routes it added) — the key to hot-disable without a
        # restart. Filled by load() here and by enable_live() at toggle time.
        self.plugin_handles: dict = {}
        self.plugins = plugin_loader.load(self.config, self.registry,
                                          handles=self.plugin_handles)
        # Idempotent status/wait tools exempt from the duplicate-call loop guard:
        # polling a job repeatedly with the same args is legitimate, not a loop.
        self._poll_safe = {t.name for t in self.registry.all()
                           if getattr(t, "poll_safe", False)}
        log.info("Discovered %d tools: %s",
                 len(self.registry.all()),
                 ", ".join(sorted(t.name for t in self.registry.all())))

        # Custom-layer description overrides (eval-tuned wording; builtin
        # tool code stays pristine).
        from runtime import tool_overrides
        tool_overrides.apply(self.registry)

        # Privacy is declared per-tool via each tool's own `private` flag — the
        # single source of truth (co-located with the tool that knows whether its
        # output is sensitive). Operators may OPTIONALLY force extra namespaces
        # private here without touching tool code; default is none.
        extra_private = set(self.config.get("privacy", {}).get("private_tool_namespaces", []) or [])
        for tool in self.registry.all():
            if tool.name.split(".", 1)[0] in extra_private:
                tool.private = True

        from runtime import gate_prompt
        self.system_prompt, _layer = gate_prompt.load(self.config,
                                                      self.config_path)

        # Runtime-loadable skills: discover once (builtin + custom layers,
        # custom wins on clashes), inject the lightweight catalog into the
        # system prompt so the model knows what it can load on demand.
        sk_cfg = self.config.get("skills", {}) or {}
        self.skills = discover_skills_layered(
            sk_cfg.get("dir", str(orch_root / "skills")), CUSTOM_SKILLS_DIR)
        self.skill_catalog = render_catalog(self.skills)

        # Hygiene: drop subcall sockets a killed process left behind (probe-
        # before-delete, so sockets of live runs in other processes survive).
        try:
            from runtime.subcall import sweep_stale_sockets
            if swept := sweep_stale_sockets():
                log.info("Swept %d stale subcall socket(s)", swept)
        except Exception:
            log.debug("subcall socket sweep failed", exc_info=True)

        self.trace = Trace(
            self.config["trace"]["db_path"],
            log_content=self.config["trace"]["log_content"],
            retention_days=self.config["trace"].get("retention_days", 0),
        )

        self.litellm_base = self.config["orchestrator"]["litellm_base"]
        self.model = self.config["orchestrator"]["model"]
        # Per-backend model-call concurrency. Local llama-servers run a fixed
        # number of slots (-np); firing more concurrent calls than slots just
        # serializes at the server and burns the request timeout while queued.
        # Map each local alias to its slot count here (cloud aliases stay unset →
        # unbounded, since that parallelism runs off-box). See _model_sem.
        self._local_concurrency = dict(
            self.config["orchestrator"].get("local_concurrency") or {})
        self._model_sems: dict[str, asyncio.Semaphore] = {}
        # Local aliases beyond the local-* prefix (see _is_local_model): the
        # local_concurrency keys are local backends by definition, so they get
        # the jinja thinking switch even without the prefix.
        self._local_aliases = frozenset(self._local_concurrency)
        # Which of those aliases actually understand the jinja thinking switch
        # (chat_template_kwargs). llama.cpp servers do; adopted vLLM/Ollama
        # endpoints (remote presets with a non-llama backend) only when the
        # admin opts in via the preset's caps.thinking — otherwise the kwarg
        # would be forwarded to a server that may reject or misread it.
        from runtime.preset_store import resolve_slot as _resolve_slot
        from runtime.preset_store import think_switch_aliases
        brain_p = _resolve_slot(self.config, "brain")
        self._think_switch_aliases = think_switch_aliases(
            self.config, self._local_aliases)
        # NOTE: orchestrator.reasoning_budget_tokens is read live per turn in
        # model_client._reasoning_budget (Config-tab override hot-applies).

        # Brain identity + capabilities, optionally read from the llama-serve.sh
        # preset that's currently serving the brain. The orchestrator talks to the
        # brain via LiteLLM (self.model stays the LiteLLM alias); the preset only
        # tells us *what* is loaded — notably whether it can see images.
        orch_cfg = self.config["orchestrator"]
        self.brain_info: dict = {}
        # ORCH_BRAIN_PRESET (env) overrides runtime.yaml's brain_preset, so the same
        # jaynet.env that drives the serving scripts can also point JayNet at
        # the active preset. Empty/unset env falls back to the YAML value.
        preset_path = env("ORCH_BRAIN_PRESET") or orch_cfg.get("brain_preset")
        if preset_path:
            from runtime.serve_preset import preset_info
            self.brain_info = preset_info(preset_path)
        vis_override = orch_cfg.get("vision")  # null=auto, true/false=force
        # An explicit preset caps.vision wins over the conf-file heuristic —
        # this is how an adopted remote server (no local .conf, no MMPROJ)
        # declares it can see images.
        vis_cap = (brain_p.get("caps") or {}).get("vision")
        auto_vision = bool(vis_cap) if vis_cap is not None \
            else bool(self.brain_info.get("vision"))
        if vis_override is None:
            self.vision_enabled = auto_vision
        else:
            self.vision_enabled = bool(vis_override)
        self.cost_table = self.config["costs"]
        self.selector = ToolSelector(self.registry, self.config)

    def refresh_plugins(self) -> None:
        """Recompute the boot-time snapshots that plugin (un)registration
        invalidates — the poll-safe tool set and the skills catalog. Called
        by the plugin hot-reload path (web/routes_plugins.py) after a live
        enable/disable; new runs see the result immediately."""
        from runtime.paths import CUSTOM_SKILLS_DIR
        self._poll_safe = {t.name for t in self.registry.all()
                           if getattr(t, "poll_safe", False)}
        orch_root = self.config_path.parent.parent
        sk_cfg = self.config.get("skills", {}) or {}
        self.skills = discover_skills_layered(
            sk_cfg.get("dir", str(orch_root / "skills")), CUSTOM_SKILLS_DIR)
        self.skill_catalog = render_catalog(self.skills)

    async def run(self, user_message: str, *, share_private: bool = False,
                  budget_overrides: dict | None = None,
                  tools: list[str] | None = None,
                  disabled_tools: set[str] | None = None,
                  auto_confirm: bool = False,
                  run_id: str | None = None,
                  on_event=None,
                  confirm_provider=None,
                  ask_provider=None,
                  history: list[dict] | None = None,
                  model: str | None = None,
                  depth: int = 0,
                  owner: str | None = None,
                  is_admin: bool = True,
                  work_root: str | None = None,
                  project_id: str | None = None,
                  extra_roots: list[str] | None = None,
                  think: bool = True,
                  extra_system: str | None = None,
                  images: list[str] | None = None,
                  run_overrides: dict | None = None,
                  verify=None,
                  base_system: str | None = None,
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
        extra_roots: additional writable roots for file tools on top of
                   work_root (e.g. a /llmwiki run's wiki dir); inherited by
                   spawned sub-agents.
        depth:     sub-agent nesting depth. 0 = top-level. Children spawned via
                   ctx.spawn run at depth+1, capped by config agent.max_depth.
        is_admin:  role of the account behind this run (security.admin_only_tools).
                   False hides those tools from selection AND refuses them at
                   dispatch. Defaults True so operator-driven paths (CLI, evals)
                   are unchanged; the web layer passes the session's real role.
        stream:    if True, the brain's model turns stream token-by-token (and
                   token/cost events are emitted). The CLI leaves this False to
                   keep the proven non-streaming path.
        run_overrides: per-run flexing of compaction/parallel_tools/sampling/
                   architect_threshold/timezone, plus "tools_patch" — per-run
                   overrides for the tools: config section (see
                   _patch_tools_config; the eval harness redirects memory/rag
                   stores into its sandbox this way) — and the internal
                   "config_patch" for other top-level sections (see
                   _patch_run_config; server-side callers only).
        """
        run_id = run_id or str(uuid.uuid4())
        eff_model = model or self.model
        b_cfg = {**self.config["budgets"], **(budget_overrides or {})}
        # Per-run overrides for context behaviour (set from the UI's Run options),
        # layered over config/runtime.yaml so the UI can flex them without a restart.
        _ro = run_overrides or {}
        eff_compaction = {**(self.config.get("compaction") or {}), **(_ro.get("compaction") or {})}
        # Complexity gate: brain rates each request 1-10 and escalates to the
        # `architect` tool at/above this threshold. Per-run override (quick
        # settings) wins over the config default; 0 disables the gate.
        eff_threshold = _ro.get("architect_threshold")
        if eff_threshold is None:
            eff_threshold = (self.config.get("architect") or {}).get("threshold", 0)
        try:
            eff_threshold = int(eff_threshold)
        except (TypeError, ValueError):
            eff_threshold = 0
        # Sampler params apply to the BRAIN only. A sub-agent on a different model
        # (e.g. the specialist.delegate specialist) keeps its own server-preset sampling — the
        # brain's config defaults and per-run overrides never touch the specialist.
        # Exception: run_overrides["sampling_force"] is the explicit opt-in for
        # callers that intentionally run a different model under pinned sampling
        # (eval benchmark variants) — chat quick-settings never set it, so the
        # impersonation invariant above stays intact.
        _ro_sampling = _ro.get("sampling") or {}
        if eff_model == self.model:
            eff_sampling = {**(self.config["orchestrator"].get("sampling") or {}),
                            **_ro_sampling}
            eff_sampling.setdefault("temperature", 0.7)   # brain fallback when config sets none
        elif _ro_sampling and _ro.get("sampling_force"):
            eff_sampling = {**(self.config["orchestrator"].get("sampling") or {}),
                            **_ro_sampling}
        else:
            eff_sampling = None
        _pt_cfg = self.config.get("parallel_tools")
        _pt_base = _pt_cfg if isinstance(_pt_cfg, dict) else {"enabled": bool(_pt_cfg)}
        eff_parallel = {**_pt_base, **(_ro.get("parallel_tools") or {})}
        warn_fraction = float(b_cfg.get("warn_fraction", 0.8) or 0)
        # Loop-guard escalation: the duplicate-call guard refuses a repeated
        # call, but a stubborn model can re-emit it (or trivial variants)
        # forever. After this many guard refusals in one run, the next turn
        # runs with tools DISABLED to force the final answer. 0 = never force.
        try:
            _lg = self.config.get("loop_guard") or {}
            guard_max = int(_lg.get("max_rejections", 6) or 0)
        except (TypeError, ValueError):
            guard_max = 6
        # Near-duplicate guard: the exact guard misses the classic overthinking
        # pattern — the SAME search reworded ("price 2026 CHF" → "24h price CHF
        # 2026"). For query-like tools, calls whose arg-token Jaccard ≥ the
        # threshold count as duplicates too (2 similar allowed, 3rd blocked).
        # 0 disables. Distinct queries score low and pass freely; very short
        # same-host URLs can look alike (tokens <3 chars are dropped).
        try:
            near_dup_threshold = float(_lg.get("near_dup_threshold", 0.75) or 0)
        except (TypeError, ValueError):
            near_dup_threshold = 0.75
        near_dup_tools = set(_lg.get("near_dup_tools")
                             or ["web.search", "web.fetch", "arxiv.search"])
        rs = RunState(budget=Budget(
            max_iterations=b_cfg["max_iterations"],
            max_wall_clock_s=b_cfg["max_wall_clock_s"],
            max_cost_usd=b_cfg["max_cost_usd"],
            max_total_tokens=b_cfg["max_total_tokens"],
            cached_token_weight=float(b_cfg.get("cached_token_weight", 0.1) or 0),
            wall_clock_grace_s=float(b_cfg.get("wall_clock_grace_s", 0) or 0),
            wall_clock_max_extensions=int(
                b_cfg.get("wall_clock_max_extensions", 0) or 0),
        ))

        self.trace.start_run(run_id, user_message, owner=owner)

        # Scratch dir (ctx.tmp_root): mid-run temp files that must not persist
        # in the project/chat workspace. With a work_root (web chat/project
        # runs) it is a STABLE per-conversation path, <work_root>/.tmp/scratch,
        # emptied here at run START (not end) — the system prompt quotes this
        # path in its workspace block, and a per-run path there broke the
        # server prompt cache for the whole replayed chat history (audit #14).
        # Without a work_root (CLI) it falls back to an ephemeral per-run
        # TemporaryDirectory, removed on ANY exit path: explicitly at run end
        # (below), or via its finalizer if setup raises before the loop's own
        # try/except takes over (mkdtemp leaked the dir on that path). The
        # work_root (project files dir, or per-chat scratch) is passed in by
        # the caller; on the CLI it's None and file tools fall back to config.
        _run_tmp_obj = None
        _run_tmp: Path | None = None
        if work_root:
            try:
                _wr = Path(work_root).resolve()
                _scratch = (_wr / ".tmp" / "scratch").resolve()
                # Defensive: never create-or-clean a path that isn't strictly
                # inside the work_root (symlinked work_root, odd mounts).
                if _scratch != _wr and _wr in _scratch.parents:
                    _scratch.mkdir(parents=True, exist_ok=True)
                    for _stale in _scratch.iterdir():
                        if _stale.is_dir() and not _stale.is_symlink():
                            shutil.rmtree(_stale, ignore_errors=True)
                        else:
                            _stale.unlink(missing_ok=True)
                    _run_tmp = _scratch
            except Exception:
                log.exception("stable scratch setup failed — per-run tmp fallback")
        if _run_tmp is None:
            _run_tmp_obj = tempfile.TemporaryDirectory(prefix=f"orchrun-{run_id[:8]}-",
                                                       ignore_cleanup_errors=True)
            _run_tmp = Path(_run_tmp_obj.name)

        # Single emit seam: writes to the trace AND (if present) to the event
        # sink. Every step in the loop goes through this, so the trace and the
        # live stream never diverge.
        _seq = {"n": 0}

        async def emit(event_type: str, iteration: int, data: dict) -> None:
            try:
                self.trace.log(run_id, event_type, iteration, data)
            except Exception:
                # Trace is the flight recorder, not the run: a brief store
                # hiccup must not kill in-flight WORK (readiness audit DB-5)
                # — same best-effort posture as the SSE sink below.
                log.exception("trace.log failed (continuing without it)")
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

        system_content = await self._system_prompt(
            extra_system=extra_system, work_root=work_root, run_tmp=_run_tmp,
            depth=depth, eff_threshold=eff_threshold, run_overrides=_ro,
            base_system=base_system)
        rs.messages = [{"role": "system", "content": system_content}]
        # Prior turns (multi-turn memory) go after the system prompt so the
        # cacheable system+tools prefix is undisturbed. Only user/assistant text
        # turns are replayed — not the internal tool-call transcript.
        # Server-side cap: the client replays its WHOLE chat with each message,
        # and every replayed turn is re-sent (re-prefilled) on every model turn
        # of the run — a long chat otherwise grows each run's cost unbounded.
        # orchestrator.max_history_messages bounds it (0 = unlimited).
        history = list(history or [])
        try:
            max_hist = int((self.config.get("orchestrator") or {}).get("max_history_messages") or 0)
        except (TypeError, ValueError):
            max_hist = 0
        if max_hist > 0 and len(history) > max_hist:
            history = history[-max_hist:]
            # Don't open the replay on a dangling assistant reply.
            while history and history[0].get("role") == "assistant":
                history.pop(0)
        for h in history:
            role = h.get("role")
            if role in ("user", "assistant") and h.get("content"):
                content = h["content"]
                # If a prior assistant turn carried a trajectory note, replay it so
                # a follow-up ("try again", "continue") knows what was already tried.
                if role == "assistant" and h.get("trajectory"):
                    content = f"{content}\n\n[Tools you ran that turn: {h['trajectory']}]"
                rs.messages.append({"role": role, "content": content})
        # The per-run datetime rides as its own one-line system message right
        # before the user's turn — NOT in the system prompt — so the volatile
        # fragment sits after the whole cacheable prefix (system + tools +
        # replayed history) and only this line plus the user message needs a
        # fresh prefill on the next run. Trailing system messages are already
        # proven on this template (budget warnings, wrap-up nudges).
        rs.messages.append({"role": "system", "content": self._datetime_note(_ro)})
        if images and self.vision_enabled:
            # OpenAI/LiteLLM multimodal: content becomes a list of blocks. The
            # text part stays first; each image rides as an image_url block. The
            # plain string `user_message` is still used for the trace, the
            # run_start event, and tool selection below.
            content_blocks: list[dict] = [{"type": "text", "text": user_message}]
            for url in images:
                content_blocks.append({"type": "image_url", "image_url": {"url": url}})
            rs.messages.append({"role": "user", "content": content_blocks})
        else:
            rs.messages.append({"role": "user", "content": user_message})
        # Track which assistant messages were derived from private tool results.
        # Indexed by message position. Used to enforce privacy on subsequent calls.
        rs.private_taint = set()
        # Track recent tool calls for loop detection: (signature, mutation
        # generation) pairs. Repeats only count within one generation — any
        # successful call by a tool NOT declared read_only bumps the generation,
        # so re-querying after a possible change is fresh information, never a
        # duplicate (a query repeated across pure queries IS still a duplicate).
        rs.recent_calls = []
        # Near-duplicate tracking for query-like tools: (name, generation,
        # arg-token set). Separate from recent_calls so the exact-signature
        # path stays untouched.
        rs.recent_query_calls = []
        rs.mutation_gen = 0
        # Compact record of what this run did, folded into the answer so a
        # follow-up turn has the trajectory (not just the final text).
        rs.trajectory = []
        # Structural record of every invoked tool (display string above is
        # truncated/hint-less; consumers like the eval harness need the full,
        # exact list).
        rs.tools_used = []

        # Select tools ONCE, before the loop starts, and freeze the set for the
        # whole run. The tool schemas are a stable prefix; keeping them constant
        # is what preserves prompt-cache hits across iterations (see guide §3.7).
        # Bounded exception: tools.load may expand the set mid-run (see the
        # ctx.expand_tools seam below) when this initial guess missed.
        # Role policy (security.admin_only_tools): a non-admin run never sees
        # these tools — the filter rides the same `disabled_tools` channel as
        # the global admin disable list (selection, tools.load expansion and
        # spawn inheritance all honor it) — and a call that slips through
        # anyway is refused at dispatch below (_admin_only_names).
        _admin_only_names: set[str] = set()
        if not is_admin:
            _patterns = (self.config.get("security") or {}).get("admin_only_tools") or []
            if _patterns:
                _admin_only_names = {t.name for t in self.registry.all()
                                     if _tool_policy_match(t.name, _patterns)}
                if _admin_only_names:
                    disabled_tools = set(disabled_tools or ()) | _admin_only_names
        rs.allowed = self.selector.select(user_message, requested=tools,
                                       disabled=disabled_tools)
        # Brain-only: under tools.code.brain_mode=verify with a coding
        # specialist present, swap code.run/execute/patch for code.check —
        # implementation routes through specialist.delegate mechanically. The same
        # predicate also bars tools.load from re-adding them mid-run.
        brain_gate = _brain_gate_active(self.config, depth)
        dispatch_gate = _brain_dispatch_active(self.config, depth)
        rs.allowed = _brain_code_gate(self.config, self.registry, rs.allowed,
                                   depth, disabled_tools)
        # /goal: a supervised run carries a declaration sink in run_overrides
        # (web/goals.py). The two verdict tools must be reachable even when the
        # auto-selector's keywords wouldn't pick them — append them to the
        # frozen set (None means "all tools", nothing to add).
        goal_sink = (_ro.get("goal") or {}).get("declarations")
        if goal_sink is not None and rs.allowed is not None:
            _known = {t.name for t in self.registry.all()}
            for _g in ("goal.complete", "goal.blocked"):
                if _g in _known and _g not in rs.allowed:
                    rs.allowed.append(_g)
        # Project-bound runs may carry tools the keyword selector can't know
        # about (plugin hooks declared them via the web layer, e.g. graphify's
        # graph.* when a project has a graph). Same shape as the goal.* block:
        # append to the frozen set — minus anything the admin disabled.
        _force = _ro.get("force_tools") or []
        if _force and rs.allowed is not None:
            _known = {t.name for t in self.registry.all()}
            for _f in _force:
                if (_f in _known and _f not in disabled_tools
                        and _f not in rs.allowed):
                    rs.allowed.append(_f)
        rs.tools_schema = self.registry.openai_schemas(rs.allowed)
        if brain_gate:
            rs.tools_schema = _brain_gate_schema_notes(rs.tools_schema,
                                                    dispatch=dispatch_gate)
        await emit("tool_selection", 0, {
            "mode": self.selector.mode,
            "requested": tools,
            "selected": rs.allowed if rs.allowed is not None else "all",
            "count": len(rs.tools_schema),
            "diag": getattr(self.selector, "_diag", None),
        })

        # Deterministic routing nudge: the same keyword signal that picked the
        # toolset also flags work that should be ROUTED, not done inline (the
        # recurring eval failure: the brain implements coding/security tasks
        # itself and never calls the specialist). Rides as its own system
        # message right before the user turn — same trick as _datetime_note —
        # so the cacheable prefix stays byte-identical. Brain-only: sub-agents
        # get narrowed toolsets and shouldn't be told to delegate.
        # Active procedure for THIS run (None when none autoloaded or sub-agent):
        # its checkpoints feed the stall ladder and the final-answer check below.
        rs.proc_name = None
        rs.proc_checkpoints = []
        if depth == 0:
            if brain_gate:
                # The standing prompt still names code.run in its verification
                # bullets — one deterministic note maps those to the gated
                # toolset instead of rewriting every bullet per mode.
                rs.messages.insert(-1, {"role": "system", "content": (
                    "Toolset note for this run: code.run/code.execute/"
                    "code.patch are NOT available to you — a coding "
                    "specialist handles implementation. Wherever your "
                    "instructions say code.run, use code.check instead "
                    "(verify-only: no network, 120s cap — tests, linters, "
                    "build checks, small python computations). Building, "
                    "fixing, installing and long dev loops go to "
                    "specialist.delegate.")})
            _nudge = await self._routing_nudge(user_message)
            if _nudge:
                rs.messages.insert(-1, {"role": "system", "content": _nudge})
            # Procedure auto-selector: a request matching a procedure's shape
            # keywords gets that procedure's body just-in-time (same placement
            # as the nudge) instead of relying on the brain to skill.load it —
            # small models rarely do. One load per run, confident matches
            # only, brain-only. Its checkpoints (if any) are kept for the
            # loop-enforced checks below: appended to stall-ladder rungs and
            # nudged once before a final answer is accepted (todo step 4).
            _proc = await self._procedure_autoload(user_message, rs.allowed)
            if _proc:
                rs.proc_name, _pbody, rs.proc_checkpoints = _proc
                rs.messages.insert(-1, {"role": "system", "content": (
                    f"Procedure auto-loaded for this request "
                    f"(skill: {rs.proc_name}) — follow its steps:\n\n{_pbody}")})
                await emit("procedure_autoload", 0, {"skill": rs.proc_name})

        # Adaptive thinking: a run the selector scored "trivial" (short request,
        # no tool keywords — conversational) skips chain-of-thought to save
        # prefill + first-token latency. Only downgrades think=True → False;
        # an explicit think=False upstream (voice, UI toggle) is already off.
        if think and (self.config.get("orchestrator") or {}).get("adaptive_thinking"):
            if (getattr(self.selector, "_diag", None) or {}).get("trivial"):
                think = False
                await emit("progress", 0, {"label": "thinking: off (trivial request)",
                                           "type": "thinking"})

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
            await emit("cost", rs.budget.iterations, {
                "model": model, "delta_usd": round(delta, 6),
                "total_usd": round(rs.budget.cost_usd, 6),
                "total_tokens": rs.budget.total_tokens,
                "tokens_prompt": rs.budget.tokens_prompt,
                "tokens_completion": rs.budget.tokens_completion,
                "tokens_cached": rs.budget.tokens_cached,
            })

        ctx = ToolContext(
            request_id=run_id,
            config=_patch_run_config(self.config, _ro.get("tools_patch"),
                                     _ro.get("config_patch")),
            budget=rs.budget,
            share_private=share_private,
            on_token=(emit_token if stream else None),
            stream=stream,
            owner=owner,
            is_admin=is_admin,
            work_root=work_root,
            project_id=project_id,
            extra_roots=extra_roots,
            tmp_root=str(_run_tmp),
            vision_enabled=self.vision_enabled,
            disabled_skills=frozenset(_ro.get("disabled_skills") or []),
        )
        # Tool-facing event emitter (e.g. deliver.files surfacing a download).
        # Reuses the loop's emit so events get trace + seq + the live sink.
        async def tool_emit(etype: str, data: dict) -> None:
            await emit(etype, rs.budget.iterations, data)
        ctx.emit = tool_emit

        # ---- Mediated sub-LLM calls from inside code.run python (RLM primitive) ----
        # Lazily-started per-run unix-socket server; the code tool mints a
        # per-execution grant (token + call cap) and injects it into the sandbox.
        # Policy, budget billing, taint gating and tracing live in
        # runtime/subcall.py — this is just the wiring. Disabled via
        # tools.code.subcalls.enabled: false.
        rs.subcall_server = None
        if (((ctx.config.get("tools") or {}).get("code") or {})
                .get("subcalls") or {}).get("enabled", True):
            from runtime.subcall import SubcallServer

            async def _subcall_grant(_limits: dict) -> dict:
                if rs.subcall_server is None:
                    rs.subcall_server = SubcallServer(
                        self, run_id=run_id, config=ctx.config,
                        default_model=eff_model,
                        tainted=lambda: bool(rs.private_taint),
                        budget=rs.budget, emit=emit, emit_cost=emit_cost)
                    await rs.subcall_server.start()
                return rs.subcall_server.mint_grant()
            ctx.subcall_grant = _subcall_grant

        # tools.load seam: mid-run toolset expansion. The frozen set is the
        # cache-stability default; this is the bounded escape hatch for when
        # the start-of-run keyword guess missed. Each expansion rebuilds the
        # schema (one prompt-cache bust), so it's capped per run.
        max_expansions = int((self.config.get("tool_selection") or {})
                             .get("max_expansions", 2))
        rs.expansions_used = 0

        async def _expand_tools(namespaces: list[str]) -> dict:
            if tools is not None:
                # A caller-fixed set (CLI --tools, a sub-agent's narrowed
                # inherit) must never widen from inside — same rule as spawn.
                return {"status": "error",
                        "error": "the tool set was fixed by the caller of this "
                                 "run and cannot be widened from inside"}
            if rs.allowed is None:
                return {"status": "ok", "loaded": [],
                        "note": "all tools are already available in this run"}
            if rs.expansions_used >= max_expansions:
                return {"status": "error",
                        "error": f"tool expansion limit reached ({max_expansions} "
                                 "per run) — continue with the tools you have, or "
                                 "ask the user to rephrase the request"}
            names = [t.name for t in self.registry.all()]
            if disabled_tools:
                names = [n for n in names if n not in disabled_tools]
            want = self.selector._expand(list(namespaces), names)
            if brain_gate:
                # The verify gate removed the brain's coding tools on purpose
                # — tools.load must not smuggle them back mid-run.
                want = {n for n in want if n not in _BRAIN_GATED_CODE_TOOLS}
            added = [n for n in names if n in want and n not in rs.allowed]
            if not added:
                have = sorted(want & set(rs.allowed))
                return {"status": "error",
                        "error": ("nothing new to load — already available: "
                                  + ", ".join(have)) if have else
                                 (f"unknown tool or category: "
                                  f"{', '.join(namespaces)}")}
            rs.allowed.extend(added)           # in-place: ctx.spawn sees it too
            rs.tools_schema = self.registry.openai_schemas(rs.allowed)
            rs.expansions_used += 1
            await emit("tool_selection", rs.budget.iterations, {
                "mode": "expanded", "added": added, "count": len(rs.tools_schema)})
            await emit("progress", rs.budget.iterations, {
                "label": f"+ tools: {', '.join(added)}", "type": "tool", "ok": True})
            return {"status": "ok", "loaded": added,
                    "note": "available from your next turn"}
        ctx.expand_tools = _expand_tools

        # Human-question seam: ask.user awaits `ctx.ask_user(questions)`. Bind the
        # provider to this run's id + emit so the request flows through the live
        # stream/trace and the eventual /api/answer resolves the right Future.
        if ask_provider is not None:
            async def _ask_user(questions, _p=ask_provider):
                return await _p.ask(run_id, questions, emit)
            ctx.ask_user = _ask_user

        # /goal verdict seam: goal.complete/goal.blocked record their declaration
        # into the supervisor's sink (read after the run). Absent on normal runs.
        if goal_sink is not None:
            def _goal_declare(status: str, text: str,
                              _sink=goal_sink) -> None:
                _sink.append({"status": status, "text": text})
            ctx.goal_declare = _goal_declare

        # ---- Sub-agent seam: ctx.spawn(...) runs a nested, bounded agent ----
        a_cfg = self.config.get("agent", {}) or {}
        max_depth = int(a_cfg.get("max_depth", 2))
        budget_obj = rs.budget                 # outer Budget (closure param shadows name)
        share_private_outer = share_private

        async def spawn(task: str, *, tools: list[str] | None = None,
                        model: str | None = None, name: str | None = None,
                        budget: dict | None = None,
                        share_private: bool | None = None,
                        verify=None, todos_sync: bool = False,
                        work_root_path: str | None = None,
                        base_system: str | None = None,
                        sampling: dict | None = None) -> dict:
            if depth + 1 > max_depth:
                return {"status": "error", "answer": "",
                        "error": f"max sub-agent depth ({max_depth}) reached; "
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
                if brain_gate:
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
            rem_wall = max(1.0, pb.max_wall_clock_s - pb.elapsed_s) if pb.max_wall_clock_s else 0.0
            # An ENABLED parent ceiling that is fully spent computes a remaining
            # allowance of 0 — and Budget.check reads a 0 ceiling as "no ceiling",
            # so carving now would hand the child an UNLIMITED budget. Refuse the
            # spawn instead (the cost/token analogue of the wall floor above). A
            # DISABLED parent dimension (0) legitimately stays unlimited below.
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
            # Config defaults (agent.default_budget) fill in any dimension the spawn
            # call didn't set, with a per-run UI override (_ro.sub_budget) layered on
            # top of config; cost/tokens/wall then fall back to the parent's remaining
            # allowance, iterations to default_sub_iterations. Every dim is still capped
            # at the parent's remaining — a child can never out-spend its parent.
            db = {**(a_cfg.get("default_budget") or {}), **(_ro.get("sub_budget") or {})}
            child_overrides = _child_budget(
                req, db,
                a_cfg.get("default_sub_iterations", 8),
                rem_cost, rem_tok, rem_wall)
            child_confirm = (_NestedConfirm(confirm_provider, emit, run_id)
                             if confirm_provider is not None else None)
            child_ask = (_NestedAsk(ask_provider, emit, run_id)
                         if ask_provider is not None else None)
            child_share = share_private if share_private is not None else share_private_outer
            # Cloud gate (audit S1): a child on a cloud brain sends its WHOLE
            # conversation off-box, so the destination alias is gated exactly
            # like an llm.call — private-tainted run needs the privacy approval
            # (never auto-confirmed), otherwise confirm_cloud_calls decides.
            # Local aliases never gate. agent.spawn and chain `agent` steps both
            # funnel through here.
            gate = cloud_gate.spawn_gate(model, self.config,
                                         private_taint=bool(rs.private_taint),
                                         share_private=child_share)
            if gate:
                gate_args = {"task": task[:500], "model": model,
                             "name": name or "sub-agent"}
                if gate == "privacy":
                    ok = await self._confirm_privacy("agent.spawn", gate_args,
                                                     run_id, emit, confirm_provider)
                    if not ok:
                        return {"status": "error", "answer": "",
                                "error": f"blocked by privacy: the conversation contains "
                                         f"private tool results and spawning a sub-agent "
                                         f"on cloud model '{model}' was not approved. Use "
                                         f"a local model instead, or ask the user to "
                                         f"enable 'share with cloud' for this run."}
                else:
                    ok = await self._confirm("agent.spawn", gate_args, run_id,
                                             auto_confirm, emit, confirm_provider)
                    if not ok:
                        return {"status": "error", "answer": "",
                                "error": f"declined: human did not approve spawning a "
                                         f"sub-agent on cloud model '{model}'"}
            await emit("subagent_start", budget_obj.iterations, {
                "name": name or "sub-agent", "depth": depth + 1,
                "model": model or self.model, "tools": child_tools,
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
            _child_wr = work_root
            if work_root_path:
                cand = Path(work_root_path).resolve()
                _roots = [Path(r).resolve() for r in
                          ([work_root] if work_root else [])
                          + [str(r) for r in (extra_roots or [])] + [str(_run_tmp)]]
                if not any(cand == r or r in cand.parents for r in _roots):
                    return {"status": "error", "answer": "",
                            "error": f"work_root_path {cand} is outside this "
                                     "run's allowed roots — refused"}
                _child_wr = str(cand)
            child = await self.run(
                task, share_private=child_share, tools=child_tools,
                disabled_tools=disabled_tools,
                auto_confirm=auto_confirm, on_event=_child_progress,
                confirm_provider=child_confirm, ask_provider=child_ask, model=model,
                depth=depth + 1, budget_overrides=child_overrides,
                # Children run STREAMED so their model turns are covered by the
                # stall watchdog — the non-streaming path has only the coarse
                # total turn timeout, so a hung child backend would otherwise
                # sit for up to turn_timeout_s. The child's token events are
                # simply ignored by the _child_progress handler above.
                owner=owner, work_root=_child_wr, extra_roots=extra_roots,
                project_id=project_id,
                # Role policy inherits: a non-admin run's children stay non-admin.
                is_admin=is_admin,
                think=think, stream=True,
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
                "name": name or "sub-agent", "depth": depth + 1,
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

        ctx.spawn = spawn

        rs.final_answer = ""
        rs.status = "ok"
        rs.error_msg = ""
        rs.budget_warned = False
        # Final-notice state: a second, blunter one-shot at
        # budget.final_warn_fraction of the WALL CLOCK (default 0.95, 0
        # disables) — the 0.8 checkpoint nudge is project-oriented ("save,
        # hand off"), but question-answering runs kept researching straight
        # through it and died on the clock with no answer at all (live:
        # gaia-dc22a632, 36 web calls, no FINAL ANSWER).
        rs.budget_final_warned = False
        # Context-pressure guard state: one-shot nudge when a turn's prompt
        # (from usage) reaches warn_fraction of the served context window —
        # the graceful alternative to the run dying on a server 400 when the
        # window actually fills. orchestrator.context_tokens 0/unset disables.
        rs.context_warned = False
        rs.last_prompt_tokens = 0
        # Loop-guard escalation state: refusals so far + whether the tools-off
        # wrap-up turn has been triggered/announced.
        rs.guard_rejections = 0
        rs.wrap_up = False
        rs.wrap_up_noted = False
        # The one-shot final-answer bounce flags (cap/trunc/empty/markup/
        # requirements/deliverable/verify-delegate/just-reply/procedure)
        # moved onto the guard instances in runtime/final_guards.py (audit
        # P2 step 2) — their rationale comments moved with them.
        # think_off_next stays here: the model-turn code reads it every
        # turn. A bounce whose retry should run with thinking OFF sets it
        # (a brain that just burned a whole completion on chain-of-thought
        # is forced into answer mode instead of being invited to think
        # again — live: gaia cap-outs died at exactly 2x max_tokens, both
        # turns pure thinking).
        rs.think_off_next = False
        # The deliverable-check config (enabled, warn_at) moved into the
        # pre-turn DeliverableReminderGuard (runtime/turn_guards.py, audit
        # P2 step 3); the final-answer DeliverableGuard reads its own key.
        rs.delegate_turn = -1          # iteration of the last coding delegation
        rs.check_turn = -1             # iteration of the last check-tool call
        # Just-reply bounce (agent.just_reply_check): compute/fresh-data
        # markers in the request + a final answer with ZERO tool calls in the
        # run → bounce once (live: just-replied "12000" for a computed 16000,
        # multi-hop answers from memory). One-shot; a stated "no tool
        # applies" clears it.
        just_reply_check = bool((self.config.get("agent") or {})
                                .get("just_reply_check", True))
        _jrk = ((self.config.get("agent") or {}).get("just_reply_keywords")
                or _DEFAULT_JUST_REPLY_KWS)
        rs.just_reply_armed = (just_reply_check and depth == 0
                            and isinstance(user_message, str)
                            and any(k in user_message.lower() for k in _jrk))
        rs.any_tool_turn = -1          # iteration of the first tool result, any tool
        rs.deliverable_warned = False
        # The failure-streak and host-give-up thresholds (loop_guard.
        # failure_nudge_after / host_give_up_after) moved into the post-tool
        # guards (runtime/turn_guards.py, audit P2 step 3) — only the shared
        # RunState init stays here.
        rs.fail_sig, rs.fail_count = None, 0
        rs.host_fails = {}
        # Delegate gate: the brain's own prompt tells it to hand non-trivial
        # coding to specialist.delegate, but small MoE brains implement inline
        # anyway (live eval: 17 inline edits, 0 delegations). Count
        # successful inline write/edit calls while delegation would actually
        # route to a specialist but stays unused; at the threshold the tool
        # result carries a directive, and with delegate_enforce inline edits
        # are REJECTED from the threshold on (after=1 + enforce = delegate
        # first, literally). Any specialist.delegate call disarms the gate.
        # 0 disables.
        try:
            delegate_after = int(_lg.get("delegate_nudge_after", 3) or 0)
        except (TypeError, ValueError):
            delegate_after = 3
        delegate_enforce = bool(_lg.get("delegate_enforce", False))
        # Soft→hard escalation (default on, brain gate only): a gated brain
        # that KEEPS writing inline after the soft directive gets write-like
        # calls REJECTED from twice the threshold on — the nudge is ignorable
        # (live: tb-regex-log wrote 4× past it), a rejection is not. Only the
        # brain's own surface narrows; children pass (depth>0), and one
        # specialist.delegate call disarms it like the enforce mode below.
        delegate_escalate = bool(_lg.get("delegate_escalate", True))
        # Available means: permitted by this run's allowlist, actually
        # registered, AND routing somewhere stronger than the default brain
        # (configured coder alias or a live coding-strength specialist —
        # the same rule specialist.delegate itself applies). Without a real route
        # the gate stays silent, so single-model installs are never forced
        # into pointless same-model child spawns.
        rs.delegate_ok = False
        if ((rs.allowed is None or not _DELEGATE_TOOLS.isdisjoint(rs.allowed))
                and any(self.registry.get(t) is not None
                        for t in _DELEGATE_TOOLS)):
            _dcfg = ((self.config.get("tools") or {}).get("code")
                     or {}).get("delegate") or {}
            if _dcfg.get("model"):
                rs.delegate_ok = True
            else:
                try:
                    from tools.model.catalog import route_strength
                    rs.delegate_ok = bool(await route_strength(self.config,
                                                            "coding"))
                except Exception:
                    rs.delegate_ok = False
        rs.inline_writes = 0
        rs.delegated = False
        # Stuck-delegate escalation: every distress hint that FIRES (failure
        # streak, host give-up, stall-ladder rung) is recorded; at
        # loop_guard.stuck_delegate_after the run gets a concrete hand-over
        # directive naming the exact specialist.delegate call, with the
        # strength picked harness-side (keyword match on the request, then
        # dominant tool activity) and checked against a live/swappable route.
        # "Consider delegating" nudges are ignorable — a spelled-out call
        # less so. No route → silence (single-model installs are never pushed
        # into same-model child spawns); one delegate call disarms it.
        _delegate_available = ((rs.allowed is None
                                or not _DELEGATE_TOOLS.isdisjoint(rs.allowed))
                               and any(self.registry.get(t) is not None
                                       for t in _DELEGATE_TOOLS))
        try:
            stuck_after = int(_lg.get("stuck_delegate_after", 3) or 0)
        except (TypeError, ValueError):
            stuck_after = 3
        rs.stuck_signals = []
        rs.stuck_fired = False
        rs.web_calls = 0

        async def _stuck_hit(source: str) -> str:
            """Record a distress signal; once the run crosses the stuck
            threshold, return the concrete hand-over directive ('' before
            that, when disabled, or when nothing routes)."""
            if (not stuck_after or rs.stuck_fired or rs.delegated or depth != 0
                    or not _delegate_available):
                return ""
            rs.stuck_signals.append(source)
            if len(rs.stuck_signals) < stuck_after:
                return ""
            msg = user_message if isinstance(user_message, str) else ""
            _skw = (((self.config.get("tool_selection") or {})
                     .get("routing_nudge") or {}).get("strength_keywords")
                    or _DEFAULT_STRENGTH_KEYWORDS)
            candidates = [tag for tag, kws in _skw.items()
                          if any(_strength_kw_hit(k, msg) for k in kws)]
            if rs.inline_writes > rs.web_calls:
                candidates.append("coding")
            if rs.web_calls:
                candidates.append("research")
            candidates += ["multi-step", "coding", "research", "allround"]
            from tools.model.catalog import strength_route
            route = None
            seen_c: set[str] = set()
            for cand in candidates:
                if cand in seen_c:
                    continue
                seen_c.add(cand)
                try:
                    plan = await strength_route(self.config, cand)
                except Exception:
                    plan = {}
                if plan:
                    route = (cand, plan)
                    break
            if not route:
                return ""
            rs.stuck_fired = True
            tag, plan = route
            mode = ("live right now" if plan.get("mode") == "live"
                    else "loadable on demand")
            return ("\n\n[system note] You are stuck ("
                    + "; ".join(rs.stuck_signals[-3:]) + "). Stop retrying "
                    "solo — hand this over NOW:\n"
                    f"specialist.delegate(task=\"<your current goal in one "
                    "or two sentences, including file paths/URLs you already "
                    f"found>\", strength=\"{tag}\")\n"
                    f"The {tag} specialist is {mode}. When it returns, "
                    "continue with its result instead of retrying the "
                    "approach that just failed.")
        # Fresh-perspective retry (GVS5H §4.4): re-delegating a task that
        # already FAILED inherits the brain's stuck framing — the reworded
        # task text anchors the child on the dead approach. Track delegated
        # task signatures and their outcomes; when the SAME task cluster comes
        # back after `after` failures, the call is rewritten to the RAW user
        # request with a de-anchoring preamble (and specialist.delegate skips its
        # orientation pack). Fires at most once per task cluster per run.
        _fr = (self.config.get("agent") or {}).get("fresh_retry") or {}
        fresh_retry_enabled = bool(_fr.get("enabled", True)) and depth == 0
        try:
            fresh_retry_after = int(_fr.get("after", 2) or 0)
        except (TypeError, ValueError):
            fresh_retry_after = 2
        rs.delegate_trials = []   # {"tokens", "failures", "fresh"}
        # Strength gate — the enforce-mode companion to the routing nudge.
        # Live evidence (run #3: 5/5 security cases stayed on the default
        # brain; one outright refusal) says the nudge alone doesn't move a
        # small MoE. When the request matches strength keywords for a tag
        # with a live OR swappable route (strength_route plan), inline
        # implementation tools are REJECTED until the first specialist.delegate
        # call (which disarms both gates and performs the swap if needed).
        # Never fires without a route — same rule as the delegate gate.
        _sg = (self.config.get("agent") or {}).get("strength_gate") or {}
        rs.strength_gate = None
        if (bool(_sg.get("enabled", True)) and depth == 0
                and (rs.allowed is None or not _DELEGATE_TOOLS.isdisjoint(rs.allowed))
                and any(self.registry.get(t) is not None
                        for t in _DELEGATE_TOOLS)
                and isinstance(user_message, str)):
            _rn = ((self.config.get("tool_selection") or {})
                   .get("routing_nudge") or {})
            _skws = _rn.get("strength_keywords") or _DEFAULT_STRENGTH_KEYWORDS
            _umsg = user_message.lower()
            for _tag, _kws in _skws.items():
                _tag = str(_tag)
                if _tag == "coding" or not any(
                        _strength_kw_hit(str(k).lower(), _umsg)
                        for k in (_kws or [])):
                    continue
                try:
                    from tools.model.catalog import strength_route as _sr
                    _plan = await _sr(self.config, _tag)
                except Exception:
                    _plan = {}
                if _plan:
                    rs.strength_gate = (_tag, str(_plan.get("alias")),
                                     str(_plan.get("mode")))
                    await emit("strength_gate", 0,
                               {"tag": _tag, "mode": _plan.get("mode"),
                                "alias": _plan.get("alias"),
                                "preset": _plan.get("preset")})
                break                       # first matching tag decides
        # Stall ladder: count consecutive turns with NO mutation (reads,
        # searches and error results don't change anything). Poll-only turns
        # (waiting on a job) are neutral — they neither count nor reset.
        # Every `after` no-progress turns one rung fires (once per run each),
        # escalating act → dumbest-version/delegate/ask → produce-or-ask.
        # agent.stall_check.enabled=false disables; 0 `after` disables.
        _sc = (self.config.get("agent") or {}).get("stall_check") or {}
        stall_enabled = bool(_sc.get("enabled", True))
        try:
            stall_after = int(_sc.get("after", 2) or 0)
        except (TypeError, ValueError):
            stall_after = 2
        rs.stall_turns = 0
        rs.stall_rung = 0
        # Badge watch: skills with `requires_badge: true` in frontmatter ask
        # the model to badge the run (run.badge) after loading — j-space's
        # eval history shows the badge step is chronically skipped (12+ of
        # 19 runs) even when everything else goes right. After such a skill
        # loads, the first file-edit tool gets a one-shot reminder until a
        # run.badge call lands. The frontmatter flag is the switch.
        rs.badge_watch = None      # name of the loaded badge-skill
        rs.badged = False
        rs.badge_nudged = False
        # Hesitation markers in the brain's own turns (overthinking signal).
        rs.overthinking_markers = 0
        # The FIRST model turn's prompt = system + tools + history + the user
        # message — the window fill /compact can shrink (later turns add this
        # run's own tool noise). Surfaced in run_finish for the UI ctx meter.
        rs.first_prompt_tokens = 0
        try:
            # run_overrides.context_tokens (the /imp ctxguard) wins over config —
            # an impersonated model usually has a different served window.
            ctx_tokens = int(_ro.get("context_tokens")
                             or (self.config.get("orchestrator") or {}).get("context_tokens") or 0)
        except (TypeError, ValueError):
            ctx_tokens = 0
        # Verifier gate (opt-in). A run with a `verify` check isn't "done" when the
        # model stops — the check must pass first. Snapshot the protected test/check
        # files now so we can detect the agent editing them to force a green.
        verify_spec = self._normalize_verify(verify)
        rs.verify_state = {"attempts": 0, "passed": False,
                        "baseline": (self._snapshot_protected(work_root, verify_spec["protect"])
                                     if verify_spec and verify_spec["protect"] else {})}
        if verify_spec is not None and verify_spec.get("hook") is None:
            # Baseline pre-run: capture the check's state BEFORE the agent
            # starts. A final failure identical to this baseline counts as
            # "not worse" in _verify — the agent is never sent chasing (or
            # blamed for) red that was already there, and can't "fix" it by
            # rewriting tests (the tamper guard above still applies).
            try:
                _pre_code, _pre_out = await self._run_verify_command(
                    verify_spec["command"],
                    Path(work_root) if work_root else Path("."),
                    verify_spec["timeout_s"], ctx)
                rs.verify_state["pre"] = {"code": _pre_code,
                                       "sig": _verify_sig(_pre_out)}
                if _pre_code != 0:
                    await emit("progress", rs.budget.iterations, {
                        "label": "verify baseline: check already fails "
                                 "(pre-existing) — 'not worse' will pass",
                        "type": "verify"})
            except Exception:
                log.exception("verify baseline pre-run failed (continuing without)")
        # Goal + progress anchor (fights goal-drift under compaction). The agent
        # keeps its note current via note.set → ctx.set_note; the loop restates
        # goal + note on every turn only when the anchor is enabled (default
        # off — see _build_anchor/_apply_anchor).
        goal_text = user_message if isinstance(user_message, str) else ""
        rs.progress = {"note": ""}
        ctx.set_note = lambda text: rs.progress.__setitem__("note", (text or "")[:4000])
        # Harness todo list (the ToDos side panel). The agent maintains it via
        # the todos tool; the loop owns the state, emits a full-snapshot `todos`
        # event on every change, and re-injects a compact rendering each turn
        # (see the anchor logic below) so compaction can't take the list away.
        rs.todo_list = TodoList()
        rs._last_todos_emit = [None]             # no-change → no re-emit (audit C1)
        rs._last_reqs_emit = [None]

        async def _todos_update(payload: dict) -> dict:
            res = rs.todo_list.apply(payload)
            if res.get("status") == "ok":
                snap = rs.todo_list.snapshot()
                reqs = list(rs.todo_list.requirements)
                if snap != rs._last_todos_emit[0] or reqs != rs._last_reqs_emit[0]:
                    rs._last_todos_emit[0] = snap
                    rs._last_reqs_emit[0] = reqs
                    await emit("todos", rs.budget.iterations,
                               {"items": snap, "requirements": reqs})
            return res
        ctx.todos_update = _todos_update
        # /goal: the "done when" criterion is an explicit requirement of every
        # supervised turn — seed it harness-side (deterministic, no model
        # cooperation needed) so the requirements gate makes the model verify
        # against it before finishing. One-shot bounce per turn; the goal
        # supervisor's own completion check stays the verdict.
        _goal_criterion = (_ro.get("goal") or {}).get("criterion")
        if _goal_criterion:
            rs.todo_list.requirements = [
                f"[must] DONE WHEN: {str(_goal_criterion)[:180]}"]
            rs._last_reqs_emit[0] = list(rs.todo_list.requirements)
            await emit("todos", rs.budget.iterations,
                       {"items": [], "requirements": list(rs.todo_list.requirements)})
        # Explicit accuracy demand in the user message ("this needs to be
        # exact", "don't guess"): seed a verification [must] harness-side —
        # same deterministic seeding as /goal's criterion above. The
        # requirements bounce then forces a verification pass before the
        # final answer instead of a single-sample guess (council-vote eval:
        # the brain answered a counting question with one code.check in 65s).
        _ag = self.config.get("agent") or {}
        if (depth == 0 and not _goal_criterion
                and bool(_ag.get("exactness_gate", True))
                and isinstance(user_message, str)):
            _ek = _ag.get("exactness_keywords") or _DEFAULT_EXACTNESS_KWS
            if any(k in user_message.lower() for k in _ek):
                _has_council = (rs.allowed is None or "council.vote" in rs.allowed) \
                    and self.registry.get("council.vote") is not None
                _how = ("council.vote self-consistency or an independent "
                        "recompute" if _has_council else
                        "an independent recompute")
                rs.todo_list.requirements = list(rs.todo_list.requirements) + [
                    f"[must] Exactness demanded: verify the answer before "
                    f"finalizing — {_how}, not a single guess"]
                rs._last_reqs_emit[0] = list(rs.todo_list.requirements)
                await emit("todos", rs.budget.iterations,
                           {"items": [],
                            "requirements": list(rs.todo_list.requirements)})
        # Working-anchor placement (off | system | trailing). Default off restores
        # the plain transcript — enable once you've confirmed your chat template
        # accepts the chosen placement. YAML `off` parses to False, so coerce.
        _am = (self.config.get("agent", {}).get("anchor", {}) or {}).get("mode", "off")
        anchor_mode = "off" if _am in (False, None, "off", "false", "") else str(_am).lower()
        # Todos re-injection when the anchor is OFF (audit T1): "trailing"
        # (default, cheap — keeps the prompt-cache prefix), "system" (fold into
        # the position-0 system message: safe on ANY chat template, at a
        # re-prefill per turn), "off" (the list lives only in the transcript
        # and the panel — no compaction protection). When the anchor is ON the
        # list always rides inside it at the anchor's placement.
        _tr = (self.config.get("agent", {}).get("anchor", {}) or {}).get("todos_reinject", "trailing")
        todos_reinject = "off" if _tr in (False, None, "off", "false", "") else str(_tr).lower()
        if todos_reinject not in ("trailing", "system", "off"):
            todos_reinject = "trailing"
        # #3 typed hand-off: files this run created/edited, surfaced to the caller.
        rs.files_touched = set()
        # Salience-aware compaction: results the agent pins via context.pin are
        # protected from stubbing regardless of age (indices are append-stable).
        rs.pinned = set()
        def _pin_last(reason=""):
            for i in range(len(rs.messages) - 1, -1, -1):
                if rs.messages[i].get("role") == "tool":
                    rs.pinned.add(i)
                    return {"pinned_index": i, "name": rs.messages[i].get("name")}
            return None
        ctx.pin_last = _pin_last
        # #1 no-progress breaker: how many times the verifier failed identically.
        rs.verify_stall = {"sig": None, "count": 0}
        # NOTE: this local was historically named `stall_after`, shadowing
        # the stall ladder's own `stall_after` parsed above (so the ladder
        # effectively read agent.verify.stall_after and agent.stall_check.
        # after was dead). Renamed for the step-3 guard extraction — both
        # keys default to 2, so default-config behavior is unchanged.
        verify_stall_after = int((self.config.get("agent", {}).get("verify", {}) or {}
                                 ).get("stall_after", 2))
        # Final-answer guards (audit P2 step 2): the bounce chain that runs
        # when the model returns text instead of tool calls, as registered
        # classes — the firing ORDER is load-bearing (see the docstring in
        # runtime/final_guards.py). The loop refreshes gctx.turn/call_think
        # before each pass. agent.max_bounces_per_answer (audit item 7) caps
        # the bounce-nudges ONE final answer may earn — each bounce costs a
        # full model turn over a growing context, so on a ~30 tok/s brain
        # the historical worst case (8+ bounces) meant minutes of stall.
        # Counted per answer (a turn with tool calls resets); 0 disables.
        try:
            max_bounces = int(a_cfg.get("max_bounces_per_answer", 3) or 0)
        except (TypeError, ValueError):
            max_bounces = 3
        gctx = GuardContext(runtime=self, ctx=ctx, cfg=a_cfg,
                            user_message=user_message, depth=depth,
                            eff_model=eff_model)
        fa_guards = [g(gctx) for g in FINAL_ANSWER_GUARDS]
        # Pre-turn and post-tool guards (audit P2 step 3): the rail-style
        # checks at turn start and after each tool result, as registered
        # classes — firing ORDER is load-bearing (see the docstring in
        # runtime/turn_guards.py). Values shared with inline code (stall
        # bookkeeping, pre-exec delegate/fresh-retry gates) are parsed
        # above and passed in; everything else each guard reads from the
        # config sections carried by the context.
        tgctx = TurnGuardContext(
            runtime=self, ctx=ctx, stuck_hit=_stuck_hit,
            user_message=user_message, depth=depth,
            warn_fraction=warn_fraction, ctx_tokens=ctx_tokens,
            budget_cfg=b_cfg, agent_cfg=a_cfg, lg_cfg=_lg,
            stall_enabled=stall_enabled, stall_after=stall_after,
            fresh_retry_enabled=fresh_retry_enabled,
            fresh_retry_after=fresh_retry_after,
            delegate_after=delegate_after,
            delegate_enforce=delegate_enforce)
        pre_turn_guards = [g(tgctx) for g in PRE_TURN_GUARDS]
        post_tool_guards = [g(tgctx) for g in POST_TOOL_GUARDS]

        try:
            while True:
                rs.budget.tick()
                # Keep the re-sent transcript from ballooning: shrink old, large
                # tool results in place (opt-in via runtime.compaction.enabled).
                # Each pass that stubs a message breaks the prompt-cache prefix
                # at that point, so the pass runs only every `every` iterations
                # (config compaction.every, default 1) — one re-prefill then
                # amortizes several stubs instead of one per turn.
                _comp_cfg = eff_compaction
                try:
                    _comp_every = int(_comp_cfg.get("every", 1) or 1)
                except (TypeError, ValueError):
                    _comp_every = 1
                if _comp_every > 1 and rs.budget.iterations % _comp_every:
                    _n_comp = 0
                else:
                    _n_comp = _compact_messages(rs.messages, _comp_cfg, rs.pinned)
                if _n_comp:
                    await emit("compaction", rs.budget.iterations, {"compacted": _n_comp})
                # Pre-turn guards (audit P2 step 3): the rail-style checks
                # that fire at turn start (budget/context pressure nudges,
                # stall ladder, deliverable early warning, loop-guard
                # wrap-up announcement), iterated in their historical firing
                # order — ORDER IS LOAD-BEARING (see runtime/turn_guards.py).
                # A fired guard returns an action; the loop appends its
                # message and emits its events here so the message/event
                # interleaving stays exactly as the inline era.
                for _pg in pre_turn_guards:
                    _act = await _pg.check(rs)
                    if _act is None:
                        continue
                    if _act.message is not None:
                        rs.messages.append({"role": "system",
                                            "content": _act.message})
                    for _ev, _data in _act.events:
                        await emit(_ev, rs.budget.iterations, _data)
                    # Telemetry (audit P2 step 4): one uniform guard_fired
                    # event per guard application, IN ADDITION to the
                    # guard's own legacy events above — gives each rail a
                    # fire rate in trace analysis.
                    await emit("guard_fired", rs.budget.iterations,
                               {"name": _pg.name, "phase": "pre_turn",
                                "turn": rs.budget.iterations})
                # ---- Model turn (streaming if a UI wants live tokens) ----
                _turn_tools = [] if rs.wrap_up else rs.tools_schema
                # Working anchor for THIS call only (never stored). Placement is
                # config-gated (default off) so a strict chat template isn't broken.
                _anchor = self._build_anchor(goal_text, rs.progress["note"],
                                             rs.todo_list.render())
                _anchor_mode = anchor_mode
                if anchor_mode == "off" and (rs.todo_list.items or rs.todo_list.requirements) and todos_reinject != "off":
                    # Anchor off, but a live todo list should still survive
                    # compaction: re-inject it alone at the configured
                    # placement (agent.anchor.todos_reinject, audit T1).
                    _anchor = self._build_todos_anchor(rs.todo_list.render())
                    _anchor_mode = todos_reinject
                elif _anchor is None and (rs.todo_list.items or rs.todo_list.requirements) and todos_reinject != "off":
                    # Anchor ON but no goal anchor (empty goal): the list still
                    # gets its re-injection, at the anchor's placement (audit T2).
                    _anchor = self._build_todos_anchor(rs.todo_list.render())
                call_messages = self._apply_anchor(rs.messages, _anchor, _anchor_mode)
                # Signal that the model call is starting — the UI shows a prefill
                # indicator so long prompts don't look hung.
                await emit("model_start", rs.budget.iterations,
                           {"model": eff_model, "stream": stream})
                call_think = think and not rs.think_off_next
                rs.think_off_next = False
                if stream:
                    turn = await self._model_turn_streaming(
                        call_messages, _turn_tools,
                        lambda t, scope="brain": emit_token(t, scope, eff_model),
                        model=eff_model, think=call_think, sampling=eff_sampling)
                else:
                    turn = await self._model_turn(call_messages, _turn_tools,
                                                  model=eff_model, think=call_think,
                                                  sampling=eff_sampling)
                # Strip any <think>…</think> from the answer text before it reaches
                # the user, history, or the trace. (Streaming already routes think
                # to the "reasoning" scope and keeps content clean; this also covers
                # the non-streaming CLI path, where content arrives whole.)
                _m = turn.get("message") or {"role": "assistant", "content": None}
                if _m.get("content"):
                    _m["content"] = _strip_think(_m["content"]) or None
                    # Overthinking signal: count hesitation markers in the
                    # brain's own content (never tool results).
                    rs.overthinking_markers += len(_OVERTHINK_RE.findall(_m["content"] or ""))
                await emit("model_turn", rs.budget.iterations, {
                    "model": eff_model,
                    "served_model": turn.get("served_model") or "",
                    "usage": turn.get("usage", {}),
                    "tool_calls": [
                        {"name": _tc_function(tc).get("name"),
                         "args": _tc_function(tc).get("arguments")}
                        for tc in (_m.get("tool_calls") or [])
                    ],
                    "content": _m.get("content") or "",
                    "content_len": len(_m.get("content") or ""),
                })

                usage = turn.get("usage", {})
                # Track the live window fill for the context-pressure guard.
                # (prompt_tokens counts THIS turn's prompt; the budget counters
                # accumulate spend across turns and can't measure the window.)
                rs.last_prompt_tokens = int(usage.get("prompt_tokens") or 0) or rs.last_prompt_tokens
                if not rs.first_prompt_tokens:
                    rs.first_prompt_tokens = int(usage.get("prompt_tokens") or 0)
                _cost_before = rs.budget.cost_usd
                rs.budget.add_usage(
                    eff_model,
                    prompt=usage.get("prompt_tokens", 0),
                    completion=usage.get("completion_tokens", 0),
                    cached=usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                            if isinstance(usage.get("prompt_tokens_details"), dict) else 0,
                    cost_table=self.cost_table,
                )
                await emit_cost(eff_model, rs.budget.cost_usd - _cost_before)

                msg = _m
                # Never replay an assistant message with NEITHER content nor
                # tool_calls: a reasoning-only turn cut at the token cap comes
                # back content=None, and re-sending it makes llama.cpp/LiteLLM
                # 400 the next request ('Assistant message must contain either
                # content or tool_calls') — the cap nudge below then killed
                # the run it was meant to rescue (readiness audit BE-8).
                if msg.get("content") is None and not msg.get("tool_calls"):
                    msg = {**msg, "content": ""}
                rs.messages.append(msg)
                tool_calls = msg.get("tool_calls") or []

                # The wrap-up turn ran with tools OFF but the model still tried
                # to call tools — cut the run rather than re-enter the guard
                # ping-pong the escalation was meant to break.
                if rs.wrap_up and tool_calls:
                    rs.status = "stuck"
                    rs.error_msg = ("loop guard: the model kept re-issuing blocked "
                                 "calls even with tools disabled")
                    rs.final_answer = (msg.get("content") or "").strip() or (
                        "[Run stopped: the model kept re-issuing blocked tool "
                        "calls instead of answering]")
                    break

                # ---- Termination: no tool calls = final answer ----
                if not tool_calls:
                    # Final-answer guards (audit P2 step 2): the bounce
                    # chain as registered classes, iterated in their
                    # historical firing order — ORDER IS LOAD-BEARING (see
                    # runtime/final_guards.py). The first guard that fires
                    # nudges and the turn restarts; each is one-shot per
                    # run. agent.max_bounces_per_answer (audit item 7) caps
                    # the bounces ONE answer may earn — at the cap the
                    # answer is accepted and a bounce_cap event names the
                    # guard that would have fired.
                    answer = msg.get("content") or ""
                    gctx.turn = turn
                    gctx.call_think = call_think
                    _fa_nudge = None
                    _fa_fired = None
                    _fa_capped = None
                    for _guard in fa_guards:
                        if _guard.pin_answer:
                            # Legacy pin point: the candidate answer becomes
                            # the run's final_answer between the requirements
                            # and deliverable guards — even when the
                            # deliverable guard is disabled.
                            rs.final_answer = answer
                        _fa_n = await _guard.check(rs, answer)
                        if _fa_n is None:
                            continue
                        if max_bounces and rs.answer_bounces >= max_bounces:
                            _fa_capped = _guard
                            break
                        _guard.fired = True
                        rs.answer_bounces += 1
                        _fa_nudge = _fa_n
                        _fa_fired = _guard
                        break
                    if _fa_capped is not None:
                        log.info("run %s: bounce cap %d reached — accepting "
                                 "the answer; suppressed guard: %s",
                                 run_id, max_bounces, _fa_capped.name)
                        rs.final_answer = answer
                        await emit("bounce_cap", rs.budget.iterations,
                                   {"guard": _fa_capped.name,
                                    "bounces": rs.answer_bounces,
                                    "cap": max_bounces})
                    elif _fa_nudge is not None:
                        if _fa_nudge.think_off:
                            rs.think_off_next = True
                        await emit(_fa_nudge.event, rs.budget.iterations,
                                   _fa_nudge.data)
                        # Telemetry (audit P2 step 4): uniform guard_fired
                        # alongside the guard's legacy event. Not emitted
                        # for a bounce-capped guard — it never applied.
                        assert _fa_fired is not None
                        await emit("guard_fired", rs.budget.iterations,
                                   {"name": _fa_fired.name,
                                    "phase": "final_answer",
                                    "turn": rs.budget.iterations})
                        rs.messages.append({"role": "user", "content":
                                            _fa_nudge.message})
                        continue
                    # Verifier gate: a text answer isn't "done" for a run that has a
                    # `verify` check — the check must pass. On failure, feed the report
                    # back and keep working (bounded by max_checks and the budget).
                    # NOT a one-shot bounce (bounded retry + its own stall
                    # breaker + break semantics), so it deliberately stays
                    # inline rather than joining the guard registry.
                    if verify_spec is not None and not rs.verify_state["passed"]:
                        _hook = verify_spec.get("hook")
                        if _hook is not None:
                            # Hook form (eval checker): the caller's own check
                            # grades the candidate answer/run state — a failing
                            # check vetoes "done" exactly like a red command.
                            ok, report = await _hook(rs.final_answer)
                        else:
                            ok, report = await self._verify(verify_spec, rs.verify_state, ctx, work_root)
                        rs.verify_state["attempts"] += 1
                        await emit("verify", rs.budget.iterations,
                                   {"ok": ok, "attempt": rs.verify_state["attempts"],
                                    "command": verify_spec["command"], "report": report[:1500]})
                        if ok:
                            rs.verify_state["passed"] = True
                            break
                        # #1 no-progress breaker: the same failure recurring means
                        # the agent isn't converging — stop early rather than burn
                        # the remaining checks/budget spinning on it.
                        sig = _verify_sig(report)
                        if sig == rs.verify_stall["sig"]:
                            rs.verify_stall["count"] += 1
                        else:
                            rs.verify_stall["sig"], rs.verify_stall["count"] = sig, 1
                        stuck = rs.verify_stall["count"] >= verify_stall_after
                        if stuck or rs.verify_state["attempts"] >= verify_spec["max_checks"]:
                            rs.status = "unverified"
                            why = (f"stuck on the same failure {rs.verify_stall['count']}× "
                                   "(not converging)" if stuck else
                                   f"did not pass after {rs.verify_state['attempts']} checks")
                            rs.error_msg = f"verifier {why}: {verify_spec['command']}"
                            rs.final_answer = ((rs.final_answer or "").rstrip()
                                            + f"\n\n[NOT VERIFIED — {why}]\n{report}")
                            await emit("verify_giveup", rs.budget.iterations,
                                       {"stuck": stuck, "attempts": rs.verify_state["attempts"]})
                            break
                        rs.messages.append({"role": "user", "content":
                            "The task is NOT complete — the verifier did not pass. Do NOT "
                            "modify the tests or the check; fix the real cause, then finish."
                            "\n\n" + report})
                        continue
                    break

                # ---- Execute tools ----
                # A turn WITH tool calls ends the current final-answer
                # sequence: the bounce cap counts per ANSWER (audit item 7),
                # not per run — work between two finish attempts starts a
                # new count.
                rs.answer_bounces = 0
                # Gating (allowlist, parse, loop-guard, privacy, confirmation) is
                # ALWAYS sequential and stateful; only execution may be parallelized
                # (opt-in: runtime.parallel_tools.enabled). We first resolve each call
                # to either a precomputed result (rejected/declined) or an approved
                # (name,args) to run, then execute, then emit/append in the ORIGINAL
                # order so the transcript, trajectory and privacy taint stay consistent.
                # Expose the taint state on ctx so cloud-reaching TOOLS (council.
                # debate, eval.compare — see runtime/cloud_gate.py) can apply the
                # same privacy rule the loop applies to llm.call below.
                ctx.private_taint = bool(rs.private_taint)
                plans: list[dict] = []
                for tc in tool_calls:
                    fn = _tc_function(tc)
                    name = fn.get("name")
                    raw_args = fn.get("arguments")
                    plan = {"tc": tc, "name": name, "args": None, "result": None}
                    if not isinstance(name, str) or not name:
                        # Malformed tool-call entry — hand the model an error
                        # result it can recover from, never a run-ending crash.
                        plan["name"] = "<malformed>"
                        plan["result"] = ToolResult(
                            status="error", result=None,
                            error=f"malformed tool call from model: "
                                  f"{repr(fn or tc)[:200]}")
                        plans.append(plan)
                        continue
                    if not is_admin and name in _admin_only_names:
                        # Role policy at execution time, not just selection:
                        # security.admin_only_tools (host shell, job/serve
                        # lifecycle, model swaps, git push, MCP, scheduling) is
                        # refused for non-admin runs even if a selection path
                        # let the name through. Checked BEFORE the allowlist so
                        # the model sees WHY the tool is unavailable.
                        plan["result"] = ToolResult(
                            status="error", result=None, tool_name=name,
                            error=f"tool '{name}' is admin-only — this account "
                                  "is not an administrator")
                        plans.append(plan)
                        continue
                    if rs.allowed is not None and name not in rs.allowed:
                        # The selected allowlist is a hard boundary, not just an
                        # exposure hint. Matters most for sub-agents — a research
                        # child literally cannot execute fs.write even if it tries.
                        plan["result"] = ToolResult(
                            status="error", result=None, tool_name=name,
                            error=f"tool '{name}' is not permitted in this run")
                        plans.append(plan)
                        continue
                    if (rs.strength_gate and not rs.delegated
                            and _gate_write_like(name, raw_args)):
                        # Strength gate: the request matched a routed strength
                        # domain with a live or swappable holder — the
                        # implementation goes through that specialist FIRST.
                        # Never a deadlock: one specialist.delegate call disarms it
                        # (sets delegated) and performs the swap if needed.
                        _gtag, _galias, _gmode = rs.strength_gate
                        if _gmode == "swap":
                            _ghold = (f"`{_galias}` holds that tag — "
                                      "specialist.delegate swaps it onto its slot")
                        elif _gmode == "allround":
                            _ghold = (f"no {_gtag}-tagged preset — the "
                                      f"allround specialist `{_galias}` takes it")
                        else:
                            _ghold = f"`{_galias}` holds that tag live"
                        plan["result"] = ToolResult(
                            status="error", result=None, tool_name=name,
                            error=f"inline implementation is closed for this "
                                  f"run — this is {_gtag} work: "
                                  f"call `specialist.delegate` with "
                                  f"strength=\"{_gtag}\" "
                                  f"({_ghold}), then verify its report")
                        plans.append(plan)
                        continue
                    # Dispatcher profile (brain_mode: dispatch): source-file
                    # writes are rejected from the FIRST call, no threshold —
                    # the brain plans/delegates/verifies and never authors
                    # code. Prose/config/data writes pass. One
                    # specialist.delegate call disarms (integration glue is a
                    # judgment call after the specialist reported).
                    if (dispatch_gate and not rs.delegated
                            and name in ("fs.write", "fs.edit")
                            and _code_file_target(raw_args)):
                        plan["result"] = ToolResult(
                            status="error", result=None, tool_name=name,
                            error="source files are closed to the "
                                  "orchestrator — hand the implementation "
                                  "to `specialist.delegate` (strength="
                                  "\"coding\"), then verify its report "
                                  "with code.check")
                        plans.append(plan)
                        continue
                    # Hard surface: enforce mode from the config threshold;
                    # the brain-gate escalation from twice it. Either way the
                    # write-like call is rejected pre-exec — the rejection IS
                    # the message, and one specialist.delegate call disarms.
                    _enforce_at = (delegate_after if delegate_enforce
                                   else 2 * delegate_after
                                   if (delegate_escalate and brain_gate) else 0)
                    if (_enforce_at and depth == 0
                            and not rs.delegated
                            and rs.inline_writes + 1 >= _enforce_at
                            and _gate_write_like(name, raw_args)
                            and rs.delegate_ok):
                        # Delegate gate, hard mode: this write would reach
                        # the threshold — reject it so the implementation
                        # goes through the specialist instead (after=1 blocks
                        # the very first inline write: delegate FIRST).
                        # Never a deadlock: one specialist.delegate call disarms.
                        plan["result"] = ToolResult(
                            status="error", result=None, tool_name=name,
                            error="inline implementation is closed for this "
                                  "run — call `specialist.delegate` with a "
                                  "complete, standalone task (the specialist "
                                  "model does the heavy lifting), then "
                                  "verify its report")
                        plans.append(plan)
                        continue
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError as e:
                        # Sanitize history: the invalid args string stays in the
                        # assistant message and is re-sent every later turn —
                        # llama-server then 500s trying to parse HISTORY tool
                        # calls (live: tb-mcmc-sampling-stan died turn 3). The
                        # error result below already carries the failure, so
                        # replace the args with valid empty JSON in place.
                        fn["arguments"] = "{}"
                        plan["result"] = ToolResult(status="error", result=None, tool_name=name,
                                                    error=f"invalid JSON args: {e}")
                        plans.append(plan)
                        continue
                    if args is None:
                        args = {}            # no-argument call: `arguments` omitted/null
                    elif not isinstance(args, dict):
                        plan["result"] = ToolResult(
                            status="error", result=None, tool_name=name,
                            error="malformed args: tool arguments must be a JSON object")
                        plans.append(plan)
                        continue
                    plan["args"] = args
                    # Fresh-perspective retry: the same task cluster delegated
                    # again after `fresh_retry_after` failed attempts →
                    # de-anchor it: the child gets the RAW user request (not
                    # the brain's stuck re-framing) and specialist.delegate skips
                    # its orientation pack. The assistant history keeps the
                    # original call; the result notes the rewrite.
                    if (fresh_retry_enabled and fresh_retry_after
                            and (name in _DELEGATE_TOOLS or name == "agent.spawn")
                            and isinstance(args.get("task"), str)):
                        _ntok = self._arg_tokens({"task": args["task"]})
                        _trial = next(
                            (t for t in rs.delegate_trials
                             if self._jaccard(t["tokens"], _ntok) >= 0.5),
                            None)
                        if (_trial is not None and not _trial["fresh"]
                                and _trial["failures"] >= fresh_retry_after):
                            _trial["fresh"] = True
                            args = dict(args)
                            args["task"] = (
                                "FRESH RETRY — earlier attempts at this task "
                                "failed. This delegation is deliberately "
                                "de-anchored: solve the ORIGINAL request below "
                                "from scratch with a DIFFERENT approach. Do "
                                "not read, patch, or build on files left by "
                                "the earlier attempts unless you have verified "
                                "they are correct.\n\nORIGINAL REQUEST:\n"
                                + (user_message or "")[:6000])
                            if name in _DELEGATE_TOOLS:
                                args["fresh"] = True
                            plan["args"] = args
                            plan["fresh_retry"] = True
                            await emit("fresh_retry", rs.budget.iterations,
                                       {"tool": name,
                                        "failures": _trial["failures"]})
                    # Strength gate assist: the gate armed on THIS run's
                    # keyword match, but small brains drop the strength=
                    # argument the rejection directive told them to pass
                    # (live: 4/4 security delegates went out without it and
                    # silently routed coding — no swap happened). The harness
                    # already knows the domain; inject it so the delegate
                    # routes (and swaps) correctly. An explicit strength=
                    # from the model always wins.
                    if (rs.strength_gate and name in _DELEGATE_TOOLS
                            and not args.get("strength")):
                        args["strength"] = rs.strength_gate[0]
                    # Loop guard — exempt poll-safe tools (job.status/logs/wait):
                    # repeatedly checking the same job while it runs is expected.
                    # Repeats count only within the current mutation generation:
                    # a query repeated after any successful non-read_only call
                    # may see NEW state, so it is never a duplicate.
                    call_sig = self._call_signature(name, args)
                    poll_exempt = name in self._poll_safe
                    sig_key = (call_sig, rs.mutation_gen)
                    if not poll_exempt and rs.recent_calls.count(sig_key) >= 2:
                        rs.guard_rejections += 1
                        # Escalation: enough refusals → the NEXT turn runs with
                        # tools disabled (the wrap-up is announced above the
                        # model-turn call). The refusal itself stays per-call.
                        if guard_max and rs.guard_rejections >= guard_max:
                            rs.wrap_up = True
                        plan["result"] = ToolResult(
                            status="error", result=None, tool_name=name,
                            error=f"duplicate tool call (loop guard): '{name}' with "
                                  "these exact args already ran twice and nothing it "
                                  "reads has changed since — the result would be "
                                  "identical. Use the earlier result and move on; "
                                  "do NOT call it again with the same args.")
                        plans.append(plan)
                        continue
                    if not poll_exempt:
                        rs.recent_calls.append(sig_key)
                        if len(rs.recent_calls) > 20:
                            rs.recent_calls.pop(0)
                    # Near-duplicate guard (query-like tools only): the exact
                    # check above misses reworded repeats — the same search
                    # with shuffled/added words. Two similar calls are fine
                    # (refinement); the third is the overthinking pattern and
                    # is blocked with a synthesize-now message.
                    if not poll_exempt and name in near_dup_tools \
                            and near_dup_threshold:
                        ntok = self._arg_tokens(args)
                        similar = sum(
                            1 for pn, pgen, ptok in rs.recent_query_calls
                            if pn == name and pgen == rs.mutation_gen
                            and self._jaccard(ptok, ntok) >= near_dup_threshold)
                        if similar >= 2:
                            rs.guard_rejections += 1
                            if guard_max and rs.guard_rejections >= guard_max:
                                rs.wrap_up = True
                            plan["result"] = ToolResult(
                                status="error", result=None, tool_name=name,
                                error=f"near-duplicate tool call (loop guard): "
                                      f"'{name}' with very similar args already "
                                      "ran twice — rewording the query will not "
                                      "produce new information. Synthesize your "
                                      "answer from the results you already have, "
                                      "or ask the user; do NOT issue another "
                                      "variant of this query.")
                            plans.append(plan)
                            continue
                        rs.recent_query_calls.append((name, rs.mutation_gen, ntok))
                        if len(rs.recent_query_calls) > 20:
                            rs.recent_query_calls.pop(0)
                    # Privacy gate: a cloud-LLM call while the conversation holds
                    # private tool results needs an explicit human ok — the request
                    # carries the full call args (the prompt), so the decision is
                    # informed. A refusal is a per-call error, never a run-ender:
                    # the model can fall back to a local tool. share_private is
                    # the blanket opt-in; auto_confirm deliberately does NOT
                    # waive this one. The check is target-aware: llm.call aimed
                    # at a local alias (incl. the vision slot for image calls)
                    # never leaves the box and never gates.
                    if not share_private and rs.private_taint and self._is_cloud_call(name, args):
                        if not await self._confirm_privacy(name, args, run_id, emit,
                                                           confirm_provider):
                            plan["result"] = ToolResult(
                                status="error", result=None, tool_name=name,
                                error="blocked by privacy: the conversation contains "
                                      "private tool results and the cloud call was not "
                                      "approved. Use a local tool/model instead, or ask "
                                      "the user to enable 'share with cloud' for this run.")
                            plans.append(plan)
                            continue
                        plan["privacy_ok"] = True   # one prompt covered both gates
                    # Confirmation gate: pause for human approval on tools that need
                    # it (job.start, git.commit, …) or that reach a cloud LLM when
                    # confirm_cloud_calls is on. No-op unless confirmation.enabled.
                    tool_obj = self.registry.get(name)
                    confirm_cloud = (self.config.get("confirmation", {}) or {}
                                     ).get("confirm_cloud_calls", True)
                    needs_confirm = (
                        (tool_obj is not None and tool_obj.needs_confirmation(args, ctx))
                        or (confirm_cloud and self._is_cloud_call(name, args)))
                    if (needs_confirm and not plan.get("privacy_ok")
                            and not await self._confirm(name, args, run_id,
                                                        auto_confirm, emit,
                                                        confirm_provider)):
                        plan["result"] = ToolResult(
                            status="error", result=None, tool_name=name,
                            error="declined: human did not approve this tool call")
                    plans.append(plan)

                # Execute approved calls — concurrently if enabled and >1 pending.
                pending = [p for p in plans if p["result"] is None]
                _pt = eff_parallel
                parallel = bool(_pt.get("enabled")) if isinstance(_pt, dict) else bool(_pt)
                if parallel and len(pending) > 1:
                    gathered = await asyncio.gather(
                        *[self._execute_tool(p["name"], p["args"], ctx) for p in pending],
                        return_exceptions=True)
                    for p, r in zip(pending, gathered):
                        p["result"] = (
                            ToolResult(status="error", result=None, tool_name=p["name"],
                                       error=f"{type(r).__name__}: {r}")
                            if isinstance(r, BaseException) else r)
                else:
                    for p in pending:
                        p["result"] = await self._execute_tool(p["name"], p["args"], ctx)

                # Emit + record + append — original tool-call order preserved.
                preview_cap = int((self.config.get("web", {}) or {}).get("tool_preview_chars", 8000))
                _mg_before = rs.mutation_gen
                for plan in plans:
                    tc = plan["tc"]; name = plan["name"]
                    args = plan["args"]; result = plan["result"]
                    await emit("tool_result", rs.budget.iterations, {
                        "tool": name,
                        "args": args,
                        "status": result.status,
                        "error": result.error,
                        "result_preview": (result.to_model_message()[:preview_cap]
                                           if result.status == "ok" else None),
                        "latency_ms": result.latency_ms,
                        "tokens": result.tokens_used,
                        "private": result.private,
                    })
                    rs.trajectory.append(_traj_entry(name, args, result))
                    rs.tools_used.append(name)
                    # Loop guard invalidation: a successful call by anything not
                    # declared read_only may have changed what later calls return
                    # (files via code.run/agent.spawn/archives/..., stores via
                    # memory.append/rag.index, services via serve.*) — bump the
                    # mutation generation so re-queries after it are fresh, not
                    # duplicates. Pure queries and poll-safe probes bump nothing.
                    if result.status == "ok" and name not in self._poll_safe:
                        _tobj = self.registry.get(name)
                        if _tobj is not None and not getattr(_tobj, "read_only", False):
                            rs.mutation_gen += 1
                    # #3 typed hand-off: remember files this run created/edited.
                    if (result.status == "ok" and name in _MUTATOR_TOOLS
                            and isinstance(args, dict)):
                        _p = (args.get("path") or args.get("to")
                              or args.get("dst") or args.get("file"))
                        if _p:
                            rs.files_touched.add(str(_p))

                    # Update budget with tool's own LLM usage (llm.call,
                    # council/eval side calls, …)
                    if result.tokens_used:
                        _tc_before = rs.budget.cost_usd
                        rs.budget.add_usage(
                            result.tokens_used.get("model", name),
                            prompt=result.tokens_used.get("prompt", 0),
                            completion=result.tokens_used.get("completion", 0),
                            cached=result.tokens_used.get("cached", 0),
                            cost_table=self.cost_table,
                        )
                        await emit_cost(result.tokens_used.get("model", name),
                                        rs.budget.cost_usd - _tc_before)

                    # Post-tool guards (audit P2 step 3): the per-result
                    # rails (failure streak, host give-up, verify-arm
                    # bookkeeping, delegate soft nudge, badge watch),
                    # iterated in their historical SIDE-EFFECT order — the
                    # hint text reassembles in the legacy fail → delegate →
                    # badge → host order via each guard's slot, so the
                    # appended content is byte-identical to the inline era
                    # (see runtime/turn_guards.py).
                    _call = ToolCallView(name=name, args=args, result=result,
                                         fresh_retry=bool(plan.get("fresh_retry")))
                    _hints: dict[str, str] = {}
                    for _tg in post_tool_guards:
                        _h = await _tg.check(rs, _call)
                        if _h is not None:
                            _hints[_tg.slot] = _h
                            # Telemetry (audit P2 step 4): uniform
                            # guard_fired alongside the legacy hint.
                            await emit("guard_fired", rs.budget.iterations,
                                       {"name": _tg.name, "phase": "post_tool",
                                        "turn": rs.budget.iterations})

                    # Append result to conversation
                    msg_idx = len(rs.messages)
                    rs.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id") if isinstance(tc, dict) else None,
                        "name": name,
                        "content": (result.to_model_message()
                                    + "".join(_hints.get(_s, "")
                                              for _s in _POST_TOOL_HINT_SLOTS)),
                    })
                    if result.private:
                        rs.private_taint.add(msg_idx)
                    # Image payload (e.g. browser.screenshot return_image): show
                    # it to the model as a follow-up user message with image
                    # blocks — only when the serving brain actually has vision.
                    if result.images and self.vision_enabled:
                        blocks = [{"type": "text",
                                   "text": f"Image output from {name}:"}]
                        blocks += [{"type": "image_url", "image_url": {"url": u}}
                                   for u in result.images]
                        rs.messages.append({"role": "user", "content": blocks})

                # Stall ladder bookkeeping: a turn that bumped the mutation
                # generation made progress (reset); a turn of ONLY poll-safe
                # probes is waiting on work already started (neutral); anything
                # else — reads, searches, errors, rejections — is a no-progress
                # turn and moves the ladder closer to its next rung.
                # Exception: product-free turns (todo/pin/badge fiddling or
                # verify-only code.check streaks) bump the mutation generation
                # but produce no work product — a brain can hide in them
                # indefinitely (live: tb-huarong, 11 todos turns, then 14
                # code.check runs of one analysis script, ladder stuck at
                # rung 1). They count as no-progress like any read.
                if stall_enabled and stall_after:
                    _no_product = bool(plans) and all(
                        p["name"] in _NO_PRODUCT_TOOLS for p in plans)
                    if rs.mutation_gen > _mg_before and not _no_product:
                        rs.stall_turns = 0
                    elif not (plans and all(
                            p["name"] in self._poll_safe for p in plans)):
                        rs.stall_turns += 1

        except asyncio.CancelledError:
            # Cancelled via the web /cancel endpoint (or task cancellation). Note:
            # detached job.* processes keep running by design — only the agent
            # loop stops. Swallow to produce a clean finish + final event.
            rs.status = "cancelled"
            rs.error_msg = "run cancelled"
            rs.final_answer = f"[Run cancelled]\nWork so far: {rs.final_answer or '(none)'}"
            log.info("Run %s cancelled", run_id)
        except BudgetExceeded as e:
            rs.status = "budget_exceeded"
            rs.error_msg = f"{e.reason}: {e.details}"
            log.warning("Budget exceeded: %s", rs.error_msg)
            rs.final_answer = (
                f"[Run terminated: {e.reason}]\n"
                f"Partial result based on work so far: {rs.final_answer or '(no answer produced yet)'}"
            )
        except ModelTurnStalled as e:
            # The brain hung (no streamed output within budgets.stall_s) or a turn
            # ran past orchestrator.turn_timeout_s — end gracefully like
            # budget_exceeded: partial work and trajectory stay usable.
            rs.status = "stalled"
            rs.error_msg = str(e)
            log.warning("Run %s stalled: %s", run_id, e)
            rs.final_answer = (
                f"[Run terminated: model stalled]\n"
                f"Partial result based on work so far: {rs.final_answer or '(no answer produced yet)'}"
            )
        except Exception as e:
            rs.status = "error"
            rs.error_msg = f"{type(e).__name__}: {e}"
            log.exception("Unexpected error in agent loop")
            # Preserve the partial answer like the budget/stall handlers do —
            # an un-retried backend blip used to REPLACE everything the run
            # had produced with the raw error (readiness audit BE-3: 172 lost
            # answers in 14 days, most of them proxy-restart ConnectErrors).
            rs.final_answer = (
                f"[Internal error: {rs.error_msg}]\n"
                f"Partial result based on work so far: {rs.final_answer or '(no answer produced yet)'}"
            )

        summary = rs.budget.summary()
        traj_str = _format_trajectory(rs.trajectory)
        # Open [must] items at the finish (requirements list + todos) — the
        # /goal supervisor reads the DONE WHEN entry as a free "not done"
        # signal instead of relying on the post-hoc judge call alone.
        open_must = [t["title"] for t in rs.todo_list.items
                     if t.get("status") in ("pending", "working")
                     and str(t.get("title") or "").lower().startswith("[must]")]
        open_must += [r for r in rs.todo_list.requirements
                      if r.lower().startswith("[must]")]
        if rs.subcall_server is not None:
            try:
                await rs.subcall_server.close()
            except Exception:
                log.exception("subcall server close failed (continuing)")
        self.trace.finish_run(run_id, rs.status, rs.final_answer, rs.error_msg, summary)
        if _run_tmp_obj is not None:
            _run_tmp_obj.cleanup()   # discard ephemeral per-run scratch (CLI fallback)
        await emit("run_finish", rs.budget.iterations, {
            "status": rs.status, "answer": rs.final_answer,
            "error": rs.error_msg or None, "budget": summary,
            "trajectory": traj_str,
            "guard_rejections": rs.guard_rejections,
            "overthinking_markers": rs.overthinking_markers,
            "prompt_tokens": rs.first_prompt_tokens,
            "context_tokens": ctx_tokens or None,
            "open_must": open_must,
        })

        return {
            "run_id": run_id,
            "status": rs.status,
            "answer": rs.final_answer,
            "error": rs.error_msg or None,
            "budget": summary,
            "trajectory": traj_str,
            "guard_rejections": rs.guard_rejections,
            "overthinking_markers": rs.overthinking_markers,
            "open_must": open_must,
            "verified": (None if verify_spec is None else rs.verify_state["passed"]),
            "verify_command": (verify_spec["command"] if verify_spec else None),
            "files_changed": sorted(rs.files_touched),
            "tools_used": rs.tools_used,
        }

    async def _system_prompt(self, *, extra_system: str | None,
                             work_root: str | None, run_tmp: Path,
                             depth: int, eff_threshold: int,
                             run_overrides: dict,
                             base_system: str | None = None) -> str:
        """Assemble the system prompt for a run.

        Everything in here is semi-static (base prompt, skill catalog,
        workspace, gate, specialist slot, location) so the whole system prefix
        — and the tool schemas the chat template renders with it — stays
        byte-identical across runs and the server prompt cache keeps hitting.
        The one per-run-varying fragment (current datetime) is NOT in here; it
        rides as a separate system message just before the user turn
        (_datetime_note), so only that line plus the user message re-prefills.

        base_system (specialist.delegate's worker mode, agent.worker_prompt):
        replace the full gate prompt with a lean worker prompt — the routing
        doctrine it teaches is the brain's job, never the worker's.
        """
        system_content = base_system if base_system is not None \
            else self.system_prompt
        # Per-run skill exclusion (eval A/B benchmark variants): the catalog
        # is re-rendered without the excluded skills; skill.load also refuses
        # them (ctx.disabled_skills). The cached full catalog is untouched.
        _ds = set((run_overrides or {}).get("disabled_skills") or [])
        if _ds:
            system_content += "\n\n" + render_catalog(
                {k: v for k, v in self.skills.items() if k not in _ds})
        elif self.skill_catalog:
            system_content += "\n\n" + self.skill_catalog
        if extra_system:
            system_content += "\n\n" + extra_system
        if work_root:
            # Tell the model its workspace root up front, so it uses relative paths from
            # turn one instead of guessing an absolute install path (e.g. the
            # ORCH_HOME tree) and bouncing off the confinement wall on the
            # first fs.* call. work_root is stable within a project/chat, so this doesn't
            # disturb the cacheable prefix across runs in the same conversation.
            from runtime.paths import HOME as _ORCH_HOME
            system_content += (
                f"\n\n— Your workspace —\nYour files this run live under `{work_root}`. "
                f"For throwaway scripts/temp files use the scratch dir `{run_tmp}` — NOT a "
                "bare `/tmp/...` path (that's outside your workspace and will be rejected). "
                "`fs.*` paths resolve relative to the workspace root — use RELATIVE paths; "
                "absolute paths outside these two roots are rejected. If you need the "
                "project's own source, it's in THIS workspace, not the live "
                f"`{_ORCH_HOME}/…` install tree. `fs.list .` / `fs.find` to orient first. "
                "Python packages: the env manager here is **uv** and venvs usually have "
                "NO pip inside — never call `.venv/bin/pip` (it doesn't exist). For a "
                "project's dependencies use the `code.deps` tool (it drives uv "
                "correctly); for a one-off, `uv pip install --python <venv-python> "
                "<pkg>`. Never install into the orchestrator's own runtime venv."
            )
        if depth == 0 and eff_threshold and 1 <= eff_threshold <= 4:
            system_content += (
                "\n\n— Complexity gate —\nBefore acting, rate this request's "
                "complexity 1-4 (1 = trivial, one obvious step · 2 = simple, a few "
                "tool calls · 3 = involved, multi-step · 4 = complex: multi-file, a "
                "real refactor, or an ambiguous design). If it is "
                f"{eff_threshold} or higher, call the `architect` tool with a "
                "complete, standalone task — it plans, has the specialist poke holes, and "
                "executes in a fresh context — instead of diving in. Below "
                f"{eff_threshold}, just handle the request directly.")
            # Soft nudge, not an auto-load: the j-space gate only works when
            # the model classifies the task itself (and its own doctrine
            # forbids loading machinery the task didn't earn).
            if "j-space" in self.skills:
                system_content += (
                    " Rated 3 or higher? That is loop-grade work — "
                    "`skill.load(\"j-space\")` earns its tokens there: gate, "
                    "ledger, and verification discipline for multi-stage tasks.")
        # Which model actually sits on the specialist slot, and what it's
        # good at — so the brain doesn't blindly specialist.delegate to a research
        # model. Semi-static (live_slot is TTL-cached, and the line is stable
        # while the slot is unchanged), so it belongs in the cacheable system
        # prefix. Probe failure → omit the line entirely.
        # Skipped in worker mode (base_system set): routing info is brain
        # doctrine — a worker must never route, and skipping both blocks keeps
        # the worker prefix stable across slot churn (cache-friendly).
        try:
            from tools.model.catalog import live_slot as _live_slot
            _slot = await _live_slot(self.config) if base_system is None else None
        except Exception:
            _slot = None
        if _slot:
            _str = ", ".join(_slot.get("strengths") or []) or "unknown"
            system_content += (f"\n\nSpecialist model: {_slot['serving']} "
                               f"(strengths: {_str})")
        # Strength-tag directory: what each registered tag means and who (if
        # anyone) currently holds it — the brain asks for a capability by tag
        # (agent.spawn strength="..."), the harness routes it mechanically.
        # Same TTL-cached probes as the slot line: semi-static, cacheable.
        try:
            from tools.model.catalog import strength_registry as _sreg
            _reg = _sreg(self.config) if base_system is None else None
        except Exception:
            _reg = None
        if _reg:
            _live: dict[str, str] = {}
            for _sn in ("specialist", "specialist2", "specialist3"):
                try:
                    _s = await _live_slot(self.config, slot=_sn)
                except Exception:
                    _s = None
                if _s and _s.get("alias"):
                    for _tag in (_s.get("strengths") or []):
                        _live.setdefault(_tag, _s["alias"])
            _parts = []
            for _tag, _desc in _reg.items():
                _holder = _live.get(_tag)
                _parts.append(f"{_tag} = {_desc}"
                              + (f" (live: {_holder})" if _holder
                                 else " (not live)"))
            system_content += (
                "\n\nStrength tags (route work with agent.spawn "
                "strength=\"<tag>\"; the harness picks the model): "
                + " · ".join(_parts))
        # User location (orchestrator.location): semi-static, so it belongs in
        # the cacheable prefix. Set → local/travel/nearby queries can assume
        # it; unset → the model asks instead of guessing.
        _loc = (self.config.get("orchestrator") or {}).get("location")
        if _loc:
            system_content += (
                f"\n\nUser location: {_loc} — assume this for local, travel, "
                "nearby, weather and price queries unless the user says otherwise.")
        else:
            system_content += (
                "\n\nUser location: unknown — if it would materially help "
                "(travel, nearby, weather, local prices), ask the user before "
                "searching.")
        # NOTE: the current datetime is deliberately NOT part of the system
        # message. It changes every run (minute resolution), which would break
        # the server prompt cache for everything rendered after it (the whole
        # replayed chat history). It is injected as a separate one-line system
        # message just before the new user turn — see _datetime_note().
        return system_content

    def _datetime_note(self, run_overrides: dict) -> str:
        """The run's 'now' as a one-line system message placed right before the
        user's message: maximal salience for time-sensitive answers, and the
        system+tools+history prefix before it stays byte-identical across runs
        (server prompt cache keeps hitting; only this line + the user turn
        re-prefills)."""
        import zoneinfo as _zi
        from datetime import datetime as _dt
        _tz_name = (run_overrides.get("timezone")
                    or (self.config.get("orchestrator") or {}).get("timezone"))
        if _tz_name:
            try:
                _now = _dt.now(_zi.ZoneInfo(_tz_name))
            except Exception:
                _now = _dt.now(UTC).astimezone()
        else:
            _now = _dt.now(UTC).astimezone()  # system timezone
        return (
            f"Current date/time: {_now.strftime('%A, %Y-%m-%d %H:%M %Z')} — "
            "this is the present; your training data is OLDER. For anything "
            "time-sensitive (prices, events, opening times, availability, "
            "versions), never answer from memory and never search for a past "
            f"year — search for the current year ({_now.year})."
        )

    @staticmethod
    def _missing_deliverables(ctx, *texts: str) -> list[str]:
        """Absolute file paths named in the task/answer that don't exist in
        the workspace. Mentioned-but-missing is the signal: input files a task
        references already exist, so a named path that doesn't is almost
        always an unwritten deliverable (the classic small-brain failure:
        solved the task, never called fs.write). Paths outside the workspace
        (/etc/...) can't be checked and are skipped; /app-style fictional
        roots rebase onto the work_root via resolve_in_roots, so container
        paths from task statements check against the real workspace."""
        from runtime.tool_base import resolve_in_roots, work_roots
        roots = work_roots(ctx)
        if not roots:
            return []
        seen: set[str] = set()
        missing: list[str] = []
        for text in texts:
            for m in _DELIVERABLE_RE.finditer(text or ""):
                p = m.group(0)
                if p in seen:
                    continue
                seen.add(p)
                try:
                    rp = resolve_in_roots(roots, p, must_exist=False)
                except (PermissionError, ValueError, OSError):
                    continue          # outside the workspace — not ours to check
                if not rp.exists():
                    missing.append(p)
                if len(missing) >= 5:
                    return missing
        return missing

    async def _strength_route_note(self, tag: str) -> str | None:
        """Routing sentence for one strength tag: delegate live when a
        specialist holding the tag is up, else a swap-in hint when a tagged
        preset exists but isn't live. None when neither applies."""
        try:
            from tools.model.catalog import route_strength, tagged_presets
        except Exception:
            return None
        try:
            live = await route_strength(self.config, tag)
        except Exception:
            live = None
        if live:
            return (f"This smells like {tag} work — delegate with "
                    f"strength=\"{tag}\" (`{live}` holds that tag live).")
        try:
            holders = [h for h in tagged_presets(self.config, tag)
                       if tag in (h.get("strengths") or [])]
        except Exception:
            holders = []
        if holders:
            names = ", ".join(h["preset"] for h in holders[:3])
            return (f"This smells like {tag} work — no model with that tag "
                    f"is live; bring one in first with `model.use` "
                    f"(swap=true): {names}.")
        return None

    async def _routing_nudge(self, user_message: str) -> str | None:
        """Per-run routing note, placed right before the user turn.

        The gate prompt's Route-don't-do doctrine is a STANDING instruction;
        small brains follow it unreliably. This adds a just-in-time, run-
        specific reminder at the position of maximal salience, driven by the
        same deterministic keyword signal as tool selection — no LLM call in
        core. A plugin may pre-empt the keyword router via the route_request
        hook (runtime/hooks.py — e.g. a decision model classifying the
        request); a routed tag skips keyword matching entirely.
        Deliberately narrower keyword sets than tool-loading: loading tools on
        a false positive is cheap, telling the brain to delegate a non-coding
        request derails it. Config: tool_selection.routing_nudge (enabled,
        code_keywords, strength_keywords). None when nothing route-worthy
        fired."""
        cfg = ((self.config.get("tool_selection") or {})
               .get("routing_nudge") or {})
        if cfg.get("enabled") is False:
            return None
        msg = (user_message or "").lower()
        if not msg:
            return None
        parts: list[str] = []
        # Plugin router first (e.g. a decision-model plugin): a confident
        # strength tag from route_request beats keyword guessing, and a
        # routed run skips the keyword router entirely (one voice, not two).
        # Fired in a thread — the ONE hook allowed bounded blocking I/O.
        routed = None
        try:
            from runtime import hooks as _hooks
            for hit in await asyncio.to_thread(
                    _hooks.fire, "route_request", user_message, self.config):
                routed = str(hit).strip()
                if routed:
                    break
        except Exception:
            routed = None
        if routed:
            note = await self._strength_route_note(routed)
            if note:
                parts.append(note)
        if parts:
            return "Routing note for THIS request: " + " ".join(parts)
        code_kws = cfg.get("code_keywords") or [
            "implement", "refactor", "debug", "compile", "traceback",
            "pytest", "write a function", "write a script", "shell script",
            "source code", "code review", "fix this code", "patch the",
            "unit test",
        ]
        if any(k in msg for k in code_kws):
            parts.append(
                "This request is coding work — Route, don't do applies: your "
                "FIRST action is `specialist.delegate` (the specialist implements); "
                "then wait for its result, verify it, deliver that. Do NOT "
                "write the implementation inline.")
        strength_kws = cfg.get("strength_keywords") or _DEFAULT_STRENGTH_KEYWORDS
        for tag, kws in strength_kws.items():
            tag = str(tag)
            # Short acronyms match on word boundaries — plain substring would
            # fire "rce" inside "source" or "cve" inside... anything; longer
            # keywords stay substring so stems work ("vuln" → "vulnerable").
            if tag == "coding" or not any(
                    _strength_kw_hit(str(k).lower(), msg)
                    for k in (kws or [])):
                continue
            note = await self._strength_route_note(tag)
            if note:
                parts.append(note)
        if not parts:
            return None
        return "Routing note for THIS request: " + " ".join(parts)

    async def _procedure_autoload(self, user_message: str,
                                  allowed) -> tuple[str, str, list[str]] | None:
        """Pick a shape-tagged procedure skill for this request, if any.

        Procedures are skills with a `shape:` frontmatter tag — distilled
        step-by-steps for a task shape (implement-from-spec, …). The skill
        catalog ASKS the model to skill.load on a match; small brains rarely
        do, so a confident keyword match loads the body for them at run
        start. Conservative by design: one shape, first match wins, never
        without skill.load in the run's toolset, no LLM call. Config:
        agent.procedure_selector (enabled, shapes). Returns
        (name, body, checkpoints) — checkpoints feed the loop's stall ladder
        and final-answer check."""
        cfg = ((self.config.get("agent") or {})
               .get("procedure_selector") or {})
        if cfg.get("enabled") is False:
            return None
        if not isinstance(user_message, str) or not user_message.strip():
            return None
        if allowed is not None and "skill.load" not in allowed:
            return None
        if self.registry.get("skill.load") is None:
            return None
        msg = user_message.lower()
        shapes = cfg.get("shapes") or _DEFAULT_PROCEDURE_SHAPES
        from runtime import paths as _paths
        from runtime.skills import discover_skills_layered_cached
        sk_dir = (self.config.get("skills") or {}).get(
            "dir", str(_paths.SKILLS_DIR))
        skills = discover_skills_layered_cached(sk_dir,
                                                _paths.CUSTOM_SKILLS_DIR)
        by_shape: dict[str, dict] = {}
        for s in skills.values():
            sh = (s.get("shape") or "").strip()
            if sh and sh not in by_shape:
                by_shape[sh] = s
        for shape, kws in shapes.items():
            if not any(str(k).lower() in msg for k in (kws or [])):
                continue
            s = by_shape.get(str(shape))
            if s:
                return s["name"], s["body"], list(s.get("checkpoints") or [])
        return None

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
            approved = cfg.get("non_interactive", "deny") == "allow"
            decision_src = f"non_interactive:{cfg.get('non_interactive', 'deny')}"
        else:
            preview = json.dumps(args, ensure_ascii=False)
            if len(preview) > 400:
                preview = preview[:400] + "…"
            prompt = (f"\n\033[33m[confirm]\033[0m {name}\n  args: {preview}\n"
                      f"  approve? [y/N] ")
            try:
                ans = await asyncio.get_running_loop().run_in_executor(None, input, prompt)
                approved = ans.strip().lower() in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                approved = False
        await emit("confirmation", 0, {"tool": name, "approved": approved, "via": decision_src})
        return approved

    async def _confirm_privacy(self, name: str, args: dict, run_id: str,
                               emit, confirm_provider=None) -> bool:
        """Human decision for a privacy-blocked cloud call (private results in
        context). Asks through the provider when one is attached and the
        confirmation system is enabled; refuses otherwise (non-interactive run,
        gate disabled) — never auto-approves."""
        cfg = self.config.get("confirmation", {}) or {}
        if confirm_provider is None or not cfg.get("enabled", True):
            return False
        reason = ("privacy: this run already saw private tool results — approving "
                  "lets this call (see its args) leave the box to a cloud LLM")
        approved = await confirm_provider.confirm(run_id, name, args, emit,
                                                  reason=reason)
        await emit("confirmation", 0, {"tool": name, "approved": approved,
                                       "via": "privacy"})
        return approved

    @staticmethod
    def _apply_anchor(messages, anchor, mode):
        """Return the message list for a model call with the working anchor placed
        per `mode`; never mutates `messages`.
          off      – no anchor (note.set becomes a no-op).
          system   – fold into the system message (position 0): safe on ANY chat
                     template, at the cost of re-prefilling each turn.
          trailing – append as a trailing system message: cheap (keeps the prompt
                     cache) but needs a template that accepts a system message last.
        """
        if not anchor or mode == "off":
            return messages
        if mode == "trailing":
            return messages + [anchor]
        if messages and messages[0].get("role") == "system":     # "system" (default)
            head = {**messages[0],
                    "content": messages[0]["content"] + "\n\n" + anchor["content"]}
            return [head] + messages[1:]
        return [anchor] + messages

    @staticmethod
    def _build_anchor(goal: str, note: str, todos: str = ""):
        """A per-turn 'working anchor' the loop appends to every model call: the
        original goal restated + the agent's live progress note + the current
        todo list. Never persisted into the transcript (so it can't be
        compacted away) — rebuilt each turn."""
        goal = (goal or "").strip()
        if not goal:
            return None
        body = "— Working anchor (always current; not part of the transcript above) —\n"
        body += "GOAL: " + (goal if len(goal) <= 700 else goal[:700] + " …")
        if note:
            body += "\n\nYOUR PROGRESS NOTES (keep current with note.set):\n" + note
        if todos:
            body += "\n\n" + todos
        body += ("\n\nStay on GOAL. If you catch yourself repeating a step that keeps "
                 "failing the same way, change approach or stop — don't spin.")
        return {"role": "system", "content": body}

    @staticmethod
    def _build_todos_anchor(todos: str):
        """Todos-only re-injection for when the working anchor is off (the
        default): the live todo list rides as a trailing system message so it
        survives compaction without folding the goal into the system prompt."""
        if not todos:
            return None
        return {"role": "system",
                "content": "— Current todo list (always up to date; not part of "
                           "the transcript above) —\n" + todos}

    def _tool_call_timeout(self, name: str) -> float:
        """Hard per-call timeout for a tool (seconds); 0 = no wrapper. Per-tool
        overrides win over the default. Tools that orchestrate sub-agents are set
        to 0 in config — they're bounded by the budget through their children, so a
        wrapper timeout would wrongly kill them mid-orchestration."""
        tcfg = self.config.get("tools", {}) or {}
        ov = tcfg.get("call_timeout_overrides") or {}
        if name not in ov and name in _DELEGATE_TOOLS:
            # Rename compat: an override written for one delegate name covers
            # the other (code.delegate is the legacy alias of
            # specialist.delegate).
            alt = next(iter(_DELEGATE_TOOLS - {name}))
            if alt in ov:
                name = alt
        raw = ov[name] if name in ov else tcfg.get("call_timeout_s", 180)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 180.0

    async def _execute_tool(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        tool = self.registry.get(name)
        if tool is None:
            return ToolResult(status="error", result=None, tool_name=name,
                              error=f"unknown tool: {name}")
        start = time.monotonic()
        timeout = self._tool_call_timeout(name)
        try:
            # Structural backstop: no single tool call may exceed its hard timeout,
            # regardless of whether the tool bounds its own I/O. On timeout the call
            # is cancelled and the run continues (the budget still bounds the loop).
            if timeout > 0:
                result = await asyncio.wait_for(tool.execute(args, ctx), timeout=timeout)
            else:
                result = await tool.execute(args, ctx)
        except TimeoutError:
            log.warning("Tool %s exceeded the %ss call timeout — cancelled", name, timeout)
            return ToolResult(status="error", result=None, tool_name=name,
                              error=f"tool timed out after {timeout:g}s (hard call limit) and was "
                                    "cancelled so the run can continue — narrow the scope or try "
                                    "a different approach",
                              latency_ms=int((time.monotonic() - start) * 1000))
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

    def _is_cloud_call(self, name: str, args: dict) -> bool:
        """True if this tool CALL would reach a remote/cloud LLM — the
        remote_llm_tools membership check plus the resolved destination
        alias, so a local-target llm.call (local-specialist, the vision
        slot for image calls) never gates (cloud_gate.remote_call_is_cloud)."""
        return cloud_gate.remote_call_is_cloud(name, args, self.config)

    @staticmethod
    def _call_signature(name: str, args: dict) -> str:
        """Stable hash of a tool call for loop detection."""
        s = name + "|" + json.dumps(args, sort_keys=True, default=str)
        return hashlib.sha256(s.encode()).hexdigest()[:16]

    @staticmethod
    def _arg_tokens(args) -> frozenset:
        """Normalized token set of a call's string VALUES (keys excluded —
        they're constant per tool and would inflate similarity). Used by the
        near-duplicate guard: reworded queries share most tokens."""
        texts: list[str] = []

        def _walk(v):
            if isinstance(v, str):
                texts.append(v)
            elif isinstance(v, dict):
                for x in v.values():
                    _walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    _walk(x)

        _walk(args)
        return frozenset(re.findall(r"[a-z0-9]{3,}", " ".join(texts).lower()))

    @staticmethod
    def _jaccard(a: frozenset, b: frozenset) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)
