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
import tempfile
import time
import uuid
from datetime import UTC
from pathlib import Path

import yaml

from runtime.env import env

from . import cloud_gate
from .budget import Budget, BudgetExceeded
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
from .selector import ToolSelector
from .skills import discover_skills_layered, render_catalog
from .todos import TodoList
from .tool_base import ToolContext, ToolResult
from .trace import Trace
from .verify import VerifyMixin, _verify_sig

log = logging.getLogger(__name__)

# Overthinking signal (arXiv 2606.00206): hesitation/branch markers in the
# brain's own assistant turns — the model reaching an answer then talking
# itself out of it. Counted per run, surfaced to the watchdog coroner.
_OVERTHINK_RE = re.compile(r"\b(?:wait|but|alternatively|hmm)\b", re.IGNORECASE)

# Tools whose success means a file was created/edited — surfaced as files_changed.
_MUTATOR_TOOLS = {"fs.write", "fs.edit", "code.patch"}


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


def _budget_warning(pressure: float, dim: str, elapsed_s: float = 0) -> str:
    """The checkpoint nudge injected once the run nears a ceiling."""
    elapsed = ""
    if elapsed_s > 0:
        m, s = divmod(int(elapsed_s), 60)
        elapsed = f" (running for {m}m {s}s)" if m else f" (running for {s}s)"
    return (
        f"\u26a0 BUDGET NOTICE: this run has used about {int(pressure * 100)}% of its "
        f"{dim} budget{elapsed} and will be cut off when it hits the limit. Do NOT start new work "
        f"or spawn new sub-tasks. Land the plane now:\n"
        f"1. Finish the current step only if it's nearly done.\n"
        f"2. Save in-progress work to the project (fs.write / deliver.files) so nothing is lost.\n"
        f"3. Write or update NEXT_STEPS.md in the project: what's done, what remains, and how "
        f"to resume in a fresh run.\n"
        f"4. Give the user a short summary of where things stand, then stop.\n"
        f"A clean hand-off beats squeezing in one more change."
    )


def _context_warning(pressure: float, ctx_tokens: int) -> str:
    """The checkpoint nudge injected once the prompt nears the model's context
    window. Without it, the first symptom of a full window is the server
    rejecting the turn (HTTP 400) and the run dying as an internal error."""
    return (
        f"\u26a0 CONTEXT NOTICE: this run's prompt has grown to about {int(pressure * 100)}% of the "
        f"model's {ctx_tokens:,}-token context window. When it fills, the run ends abruptly. "
        "Change gear now:\n"
        "1. Do NOT re-read large files or long outputs — work from what is already in context.\n"
        "2. Save in-progress work to the project (fs.write / deliver.files) so nothing is lost.\n"
        "3. Write or update NEXT_STEPS.md in the project: what's done, what remains, and how "
        "to resume in a fresh run.\n"
        "4. Give the user a short summary of where things stand, then stop.\n"
        "A clean hand-off beats filling the window mid-edit."
    )


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


# Inline file-writing tools the delegate gate watches: a brain racking these
# up while a coder specialist sits unused is doing the specialist's job.
_DELEGATE_GATE_TOOLS = frozenset({"fs.write", "fs.edit", "code.patch"})


def _exec_failure(name: str, result) -> tuple[bool, str | None]:
    """(failed, signature) for execution-style tools. These report command
    failures in their PAYLOAD (ok:false / exit_code != 0), not as tool errors
    — a non-zero exit is normal signal to the model. The signature (tool,
    exit code, normalized last stderr line) is deliberately stable across
    'fix' attempts that hit the same crash: digits and addresses vary between
    builds, the crash doesn't. Exactly the loop the escalation nudge breaks."""
    r = result.result if isinstance(result.result, dict) else {}
    if result.status == "error":
        rc, err = None, str(result.error or "")
    elif r.get("ok") is False or r.get("exit_code") not in (None, 0):
        rc = r.get("exit_code")
        err = str(r.get("stderr") or r.get("error") or "")
    else:
        return False, None
    line = ""
    for ln in reversed(err.strip().splitlines()):
        if ln.strip():
            line = ln.strip()
            break
    norm = re.sub(r"0x[0-9a-fA-F]+", "0x", line.lower())
    norm = re.sub(r"\d+", "#", norm)
    norm = re.sub(r"\s+", " ", norm)[:80]
    return True, f"{name}|{rc}|{norm}"


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
                confirm_provider=None, ask_provider=None, emit=None):
    """Build a ctx.spawn for contexts WITHOUT a parent agent run (slash commands).

    A slashed `/<tool>` executes in a bare ToolContext, so spawn-dependent tools
    (code.delegate, agent.spawn, architect, …) died with "sub-agents are not
    available". The returned callable runs the child as a depth-1 agent via
    runtime.run: config `agent.default_budget` caps it (the call's `budget` arg
    wins per dimension), confirmations/asks route to the caller's providers
    against its run_id, and child steps forward as progress lines. There is no
    parent budget to reconcile into — the config ceilings are the only clamp.
    """
    async def spawn(task: str, *, tools: list[str] | None = None,
                    model: str | None = None, name: str | None = None,
                    budget: dict | None = None,
                    share_private: bool | None = None, verify=None,
                    todos_sync: bool = False,
                    work_root_path: str | None = None) -> dict:
        # todos_sync is accepted for signature parity with the loop's spawn;
        # the slash path has no parent list, so child todos events simply
        # forward to the stream (no state to sync).
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
            work_root=child_wr,
            confirm_provider=child_confirm,
            ask_provider=child_ask,
            on_event=_child_progress_fwd(_emit) if emit is not None else None,
            # Streamed so the stall watchdog covers the child's model turns —
            # same reasoning as the loop's own spawn.
            stream=True,
            verify=verify,
        )
        await _emit("subagent_finish", {"name": name or "sub-agent", "depth": 1,
                                        "status": child.get("status"),
                                        "sub_run_id": child.get("run_id"),
                                        "budget": child.get("budget", {})})
        return child

    return spawn




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
        self.connector_rows = _connectors.refresh(self.registry)
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
                  work_root: str | None = None,
                  project_id: str | None = None,
                  extra_roots: list[str] | None = None,
                  think: bool = True,
                  extra_system: str | None = None,
                  images: list[str] | None = None,
                  run_overrides: dict | None = None,
                  verify=None,
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
        # (e.g. the code.delegate specialist) keeps its own server-preset sampling — the
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
        budget = Budget(
            max_iterations=b_cfg["max_iterations"],
            max_wall_clock_s=b_cfg["max_wall_clock_s"],
            max_cost_usd=b_cfg["max_cost_usd"],
            max_total_tokens=b_cfg["max_total_tokens"],
            cached_token_weight=float(b_cfg.get("cached_token_weight", 0.1) or 0),
        )

        self.trace.start_run(run_id, user_message, owner=owner)

        # Ephemeral per-run scratch (ctx.tmp_root): mid-run temp files that must
        # not persist in the project/chat workspace. TemporaryDirectory removes it
        # on ANY exit path: explicitly at run end (below), or via its finalizer if
        # setup raises before the loop's own try/except takes over (mkdtemp leaked
        # the dir on that path). The work_root (project files dir, or per-chat
        # scratch) is passed in by the caller; on the CLI it's None and file tools
        # fall back to config.
        _run_tmp_obj = tempfile.TemporaryDirectory(prefix=f"orchrun-{run_id[:8]}-",
                                                   ignore_cleanup_errors=True)
        _run_tmp = Path(_run_tmp_obj.name)

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

        system_content = await self._system_prompt(
            extra_system=extra_system, work_root=work_root, run_tmp=_run_tmp,
            depth=depth, eff_threshold=eff_threshold, run_overrides=_ro)
        messages: list[dict] = [{"role": "system", "content": system_content}]
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
                messages.append({"role": role, "content": content})
        # The per-run datetime rides as its own one-line system message right
        # before the user's turn — NOT in the system prompt — so the volatile
        # fragment sits after the whole cacheable prefix (system + tools +
        # replayed history) and only this line plus the user message needs a
        # fresh prefill on the next run. Trailing system messages are already
        # proven on this template (budget warnings, wrap-up nudges).
        messages.append({"role": "system", "content": self._datetime_note(_ro)})
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
        # Track recent tool calls for loop detection: (signature, mutation
        # generation) pairs. Repeats only count within one generation — any
        # successful call by a tool NOT declared read_only bumps the generation,
        # so re-querying after a possible change is fresh information, never a
        # duplicate (a query repeated across pure queries IS still a duplicate).
        recent_calls: list[tuple[str, int]] = []
        # Near-duplicate tracking for query-like tools: (name, generation,
        # arg-token set). Separate from recent_calls so the exact-signature
        # path stays untouched.
        recent_query_calls: list[tuple[str, int, frozenset]] = []
        mutation_gen = 0
        # Compact record of what this run did, folded into the answer so a
        # follow-up turn has the trajectory (not just the final text).
        trajectory: list[str] = []
        # Structural record of every invoked tool (display string above is
        # truncated/hint-less; consumers like the eval harness need the full,
        # exact list).
        tools_used: list[str] = []

        # Select tools ONCE, before the loop starts, and freeze the set for the
        # whole run. The tool schemas are a stable prefix; keeping them constant
        # is what preserves prompt-cache hits across iterations (see guide §3.7).
        # Bounded exception: tools.load may expand the set mid-run (see the
        # ctx.expand_tools seam below) when this initial guess missed.
        allowed = self.selector.select(user_message, requested=tools,
                                       disabled=disabled_tools)
        # /goal: a supervised run carries a declaration sink in run_overrides
        # (web/goals.py). The two verdict tools must be reachable even when the
        # auto-selector's keywords wouldn't pick them — append them to the
        # frozen set (None means "all tools", nothing to add).
        goal_sink = (_ro.get("goal") or {}).get("declarations")
        if goal_sink is not None and allowed is not None:
            _known = {t.name for t in self.registry.all()}
            for _g in ("goal.complete", "goal.blocked"):
                if _g in _known and _g not in allowed:
                    allowed.append(_g)
        # Project-bound runs may carry tools the keyword selector can't know
        # about (plugin hooks declared them via the web layer, e.g. graphify's
        # graph.* when a project has a graph). Same shape as the goal.* block:
        # append to the frozen set — minus anything the admin disabled.
        _force = _ro.get("force_tools") or []
        if _force and allowed is not None:
            _known = {t.name for t in self.registry.all()}
            for _f in _force:
                if (_f in _known and _f not in disabled_tools
                        and _f not in allowed):
                    allowed.append(_f)
        tools_schema = self.registry.openai_schemas(allowed)
        await emit("tool_selection", 0, {
            "mode": self.selector.mode,
            "requested": tools,
            "selected": allowed if allowed is not None else "all",
            "count": len(tools_schema),
            "diag": getattr(self.selector, "_diag", None),
        })

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
            await emit("cost", budget.iterations, {
                "model": model, "delta_usd": round(delta, 6),
                "total_usd": round(budget.cost_usd, 6),
                "total_tokens": budget.total_tokens,
                "tokens_prompt": budget.tokens_prompt,
                "tokens_completion": budget.tokens_completion,
                "tokens_cached": budget.tokens_cached,
            })

        ctx = ToolContext(
            request_id=run_id,
            config=_patch_run_config(self.config, _ro.get("tools_patch"),
                                     _ro.get("config_patch")),
            budget=budget,
            share_private=share_private,
            on_token=(emit_token if stream else None),
            stream=stream,
            owner=owner,
            work_root=work_root,
            project_id=project_id,
            extra_roots=extra_roots,
            tmp_root=str(_run_tmp),
            vision_enabled=self.vision_enabled,
        )
        # Tool-facing event emitter (e.g. deliver.files surfacing a download).
        # Reuses the loop's emit so events get trace + seq + the live sink.
        async def tool_emit(etype: str, data: dict) -> None:
            await emit(etype, budget.iterations, data)
        ctx.emit = tool_emit

        # ---- Mediated sub-LLM calls from inside code.execute (RLM primitive) ----
        # Lazily-started per-run unix-socket server; code.execute mints a
        # per-execution grant (token + call cap) and injects it into the sandbox.
        # Policy, budget billing, taint gating and tracing live in
        # runtime/subcall.py — this is just the wiring. Disabled via
        # tools.code.subcalls.enabled: false.
        subcall_server = None
        if (((ctx.config.get("tools") or {}).get("code") or {})
                .get("subcalls") or {}).get("enabled", True):
            from runtime.subcall import SubcallServer

            async def _subcall_grant(_limits: dict) -> dict:
                nonlocal subcall_server
                if subcall_server is None:
                    subcall_server = SubcallServer(
                        self, run_id=run_id, config=ctx.config,
                        default_model=eff_model,
                        tainted=lambda: bool(private_taint),
                        budget=budget, emit=emit, emit_cost=emit_cost)
                    await subcall_server.start()
                return subcall_server.mint_grant()
            ctx.subcall_grant = _subcall_grant

        # tools.load seam: mid-run toolset expansion. The frozen set is the
        # cache-stability default; this is the bounded escape hatch for when
        # the start-of-run keyword guess missed. Each expansion rebuilds the
        # schema (one prompt-cache bust), so it's capped per run.
        max_expansions = int((self.config.get("tool_selection") or {})
                             .get("max_expansions", 2))
        expansions_used = 0

        async def _expand_tools(namespaces: list[str]) -> dict:
            nonlocal tools_schema, expansions_used
            if tools is not None:
                # A caller-fixed set (CLI --tools, a sub-agent's narrowed
                # inherit) must never widen from inside — same rule as spawn.
                return {"status": "error",
                        "error": "the tool set was fixed by the caller of this "
                                 "run and cannot be widened from inside"}
            if allowed is None:
                return {"status": "ok", "loaded": [],
                        "note": "all tools are already available in this run"}
            if expansions_used >= max_expansions:
                return {"status": "error",
                        "error": f"tool expansion limit reached ({max_expansions} "
                                 "per run) — continue with the tools you have, or "
                                 "ask the user to rephrase the request"}
            names = [t.name for t in self.registry.all()]
            if disabled_tools:
                names = [n for n in names if n not in disabled_tools]
            want = self.selector._expand(list(namespaces), names)
            added = [n for n in names if n in want and n not in allowed]
            if not added:
                have = sorted(want & set(allowed))
                return {"status": "error",
                        "error": ("nothing new to load — already available: "
                                  + ", ".join(have)) if have else
                                 (f"unknown tool or category: "
                                  f"{', '.join(namespaces)}")}
            allowed.extend(added)           # in-place: ctx.spawn sees it too
            tools_schema = self.registry.openai_schemas(allowed)
            expansions_used += 1
            await emit("tool_selection", budget.iterations, {
                "mode": "expanded", "added": added, "count": len(tools_schema)})
            await emit("progress", budget.iterations, {
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
        budget_obj = budget                 # outer Budget (closure param shadows name)
        share_private_outer = share_private

        async def spawn(task: str, *, tools: list[str] | None = None,
                        model: str | None = None, name: str | None = None,
                        budget: dict | None = None,
                        share_private: bool | None = None,
                        verify=None, todos_sync: bool = False,
                        work_root_path: str | None = None) -> dict:
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
                    if tools and not child_tools:
                        # An explicit request that intersects to NOTHING must not
                        # silently run with a broader (or auto-selected) toolset.
                        return {"status": "error", "answer": "",
                                "error": f"none of the requested tools {tools} are "
                                         f"permitted in this run — permitted: "
                                         f"{', '.join(sorted(allowed))}"}
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
                                         private_taint=bool(private_taint),
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
                todo_list.replace(items)
            _child_progress = _child_progress_fwd(
                _child_emit,
                on_todos=_sync_child_todos if todos_sync else None,
                forward_todos=todos_sync)
            # Optional per-child workspace override (e.g. code.delegate's
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
                think=think, stream=True,
                verify=verify,
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

        final_answer = ""
        status = "ok"
        error_msg = ""
        budget_warned = False
        # Context-pressure guard state: one-shot nudge when a turn's prompt
        # (from usage) reaches warn_fraction of the served context window —
        # the graceful alternative to the run dying on a server 400 when the
        # window actually fills. orchestrator.context_tokens 0/unset disables.
        context_warned = False
        last_prompt_tokens = 0
        # Loop-guard escalation state: refusals so far + whether the tools-off
        # wrap-up turn has been triggered/announced.
        guard_rejections = 0
        wrap_up = False
        wrap_up_noted = False
        # One-shot nudge for a generation cut at the completion cap during
        # reasoning (finish 'length', no content): give the model one chance
        # to answer briefly instead of ending the run with an empty answer.
        cap_nudged = False
        # Crash/failure-loop escalation: execution tools (code.run/code.execute)
        # report command failures in their PAYLOAD (ok:false / exit_code!=0), not
        # as tool errors — so a crash-retry loop (a segfaulting solver rebuilt
        # 70× in a live bench run) trips no duplicate guard. Track consecutive
        # same-signature failures; at the threshold the tool result gets a
        # strategy-change hint appended. 0 disables.
        try:
            fail_nudge_after = int(_lg.get("failure_nudge_after", 3) or 0)
        except (TypeError, ValueError):
            fail_nudge_after = 3
        fail_nudge_tools = set(_lg.get("failure_nudge_tools")
                               or ["code.run", "code.execute"])
        fail_sig, fail_count = None, 0
        # Delegate gate: the brain's own prompt tells it to hand non-trivial
        # coding to code.delegate, but small MoE brains implement inline
        # anyway (live eval: 17 inline edits, 0 delegations). Count
        # successful inline write/edit calls while delegation would actually
        # route to a specialist but stays unused; at the threshold the tool
        # result carries a directive, and with delegate_enforce inline edits
        # are REJECTED from the threshold on (after=1 + enforce = delegate
        # first, literally). Any code.delegate call disarms the gate.
        # 0 disables.
        try:
            delegate_after = int(_lg.get("delegate_nudge_after", 3) or 0)
        except (TypeError, ValueError):
            delegate_after = 3
        delegate_enforce = bool(_lg.get("delegate_enforce", False))
        # Available means: permitted by this run's allowlist, actually
        # registered, AND routing somewhere stronger than the default brain
        # (configured coder alias or a live coding-strength specialist —
        # the same rule code.delegate itself applies). Without a real route
        # the gate stays silent, so single-model installs are never forced
        # into pointless same-model child spawns.
        delegate_ok = False
        if ((allowed is None or "code.delegate" in allowed)
                and self.registry.get("code.delegate") is not None):
            _dcfg = ((self.config.get("tools") or {}).get("code")
                     or {}).get("delegate") or {}
            if _dcfg.get("model"):
                delegate_ok = True
            else:
                try:
                    from tools.model.catalog import route_strength
                    delegate_ok = bool(await route_strength(self.config,
                                                            "coding"))
                except Exception:
                    delegate_ok = False
        inline_writes = 0
        delegated = False
        # Hesitation markers in the brain's own turns (overthinking signal).
        overthinking_markers = 0
        # The FIRST model turn's prompt = system + tools + history + the user
        # message — the window fill /compact can shrink (later turns add this
        # run's own tool noise). Surfaced in run_finish for the UI ctx meter.
        first_prompt_tokens = 0
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
        verify_state = {"attempts": 0, "passed": False,
                        "baseline": (self._snapshot_protected(work_root, verify_spec["protect"])
                                     if verify_spec else {})}
        if verify_spec is not None:
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
                verify_state["pre"] = {"code": _pre_code,
                                       "sig": _verify_sig(_pre_out)}
                if _pre_code != 0:
                    await emit("progress", budget.iterations, {
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
        progress = {"note": ""}
        ctx.set_note = lambda text: progress.__setitem__("note", (text or "")[:4000])
        # Harness todo list (the ToDos side panel). The agent maintains it via
        # the todos tool; the loop owns the state, emits a full-snapshot `todos`
        # event on every change, and re-injects a compact rendering each turn
        # (see the anchor logic below) so compaction can't take the list away.
        todo_list = TodoList()
        _last_todos_emit = [None]             # no-change → no re-emit (audit C1)

        async def _todos_update(payload: dict) -> dict:
            res = todo_list.apply(payload)
            if res.get("status") == "ok":
                snap = todo_list.snapshot()
                if snap != _last_todos_emit[0]:
                    _last_todos_emit[0] = snap
                    await emit("todos", budget.iterations, {"items": snap})
            return res
        ctx.todos_update = _todos_update
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
        files_touched: set[str] = set()
        # Salience-aware compaction: results the agent pins via context.pin are
        # protected from stubbing regardless of age (indices are append-stable).
        pinned: set[int] = set()
        def _pin_last(reason=""):
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "tool":
                    pinned.add(i)
                    return {"pinned_index": i, "name": messages[i].get("name")}
            return None
        ctx.pin_last = _pin_last
        # #1 no-progress breaker: how many times the verifier failed identically.
        verify_stall = {"sig": None, "count": 0}
        stall_after = int((self.config.get("agent", {}).get("verify", {}) or {}
                           ).get("stall_after", 2))

        try:
            while True:
                budget.tick()
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
                if _comp_every > 1 and budget.iterations % _comp_every:
                    _n_comp = 0
                else:
                    _n_comp = _compact_messages(messages, _comp_cfg, pinned)
                if _n_comp:
                    await emit("compaction", budget.iterations, {"compacted": _n_comp})
                # Once the run nears any ceiling, nudge the model to land the
                # plane: save progress, leave a resume note, summarize, and stop —
                # instead of getting hard-cut mid-edit with nothing usable.
                if not budget_warned and warn_fraction:
                    pr, dim = budget.pressure()
                    if pr >= warn_fraction:
                        budget_warned = True
                        messages.append({"role": "system", "content": _budget_warning(pr, dim, budget.elapsed_s)})
                        await emit("budget_warning", budget.iterations,
                                   {"pressure": round(pr, 2), "dimension": dim,
                                    "elapsed_s": round(budget.elapsed_s, 1)})
                # Same one-shot nudge when the PROMPT itself nears the context
                # window — distinct from the token BUDGET (cumulative spend);
                # this is about the per-turn window filling up.
                if (not context_warned and warn_fraction and ctx_tokens
                        and last_prompt_tokens / ctx_tokens >= warn_fraction):
                    context_warned = True
                    cpr = last_prompt_tokens / ctx_tokens
                    messages.append({"role": "system",
                                     "content": _context_warning(cpr, ctx_tokens)})
                    await emit("context_warning", budget.iterations,
                               {"pressure": round(cpr, 2),
                                "context_tokens": ctx_tokens,
                                "prompt_tokens": last_prompt_tokens})
                # ---- Model turn (streaming if a UI wants live tokens) ----
                # Loop-guard escalation: after guard_max refusals the model gets
                # ONE turn with tools disabled to force the answer it owes.
                if wrap_up and not wrap_up_noted:
                    wrap_up_noted = True
                    messages.append({"role": "system", "content": (
                        f"LOOP GUARD: you re-issued blocked duplicate tool calls "
                        f"{guard_rejections}×. Tool use is now DISABLED for the "
                        "rest of this run. Give your final answer immediately "
                        "from the results already gathered — say plainly what "
                        "you found and what you could not verify.")})
                    await emit("progress", budget.iterations, {
                        "label": f"loop guard: {guard_rejections} blocked duplicates "
                                 "— tools off, forcing the final answer",
                        "type": "guard"})
                _turn_tools = [] if wrap_up else tools_schema
                # Working anchor for THIS call only (never stored). Placement is
                # config-gated (default off) so a strict chat template isn't broken.
                _anchor = self._build_anchor(goal_text, progress["note"],
                                             todo_list.render())
                _anchor_mode = anchor_mode
                if anchor_mode == "off" and todo_list.items and todos_reinject != "off":
                    # Anchor off, but a live todo list should still survive
                    # compaction: re-inject it alone at the configured
                    # placement (agent.anchor.todos_reinject, audit T1).
                    _anchor = self._build_todos_anchor(todo_list.render())
                    _anchor_mode = todos_reinject
                elif _anchor is None and todo_list.items and todos_reinject != "off":
                    # Anchor ON but no goal anchor (empty goal): the list still
                    # gets its re-injection, at the anchor's placement (audit T2).
                    _anchor = self._build_todos_anchor(todo_list.render())
                call_messages = self._apply_anchor(messages, _anchor, _anchor_mode)
                # Signal that the model call is starting — the UI shows a prefill
                # indicator so long prompts don't look hung.
                await emit("model_start", budget.iterations,
                           {"model": eff_model, "stream": stream})
                if stream:
                    turn = await self._model_turn_streaming(
                        call_messages, _turn_tools,
                        lambda t, scope="brain": emit_token(t, scope, eff_model),
                        model=eff_model, think=think, sampling=eff_sampling)
                else:
                    turn = await self._model_turn(call_messages, _turn_tools,
                                                  model=eff_model, think=think,
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
                    overthinking_markers += len(_OVERTHINK_RE.findall(_m["content"] or ""))
                await emit("model_turn", budget.iterations, {
                    "model": eff_model,
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
                last_prompt_tokens = int(usage.get("prompt_tokens") or 0) or last_prompt_tokens
                if not first_prompt_tokens:
                    first_prompt_tokens = int(usage.get("prompt_tokens") or 0)
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

                msg = _m
                messages.append(msg)
                tool_calls = msg.get("tool_calls") or []

                # The wrap-up turn ran with tools OFF but the model still tried
                # to call tools — cut the run rather than re-enter the guard
                # ping-pong the escalation was meant to break.
                if wrap_up and tool_calls:
                    status = "stuck"
                    error_msg = ("loop guard: the model kept re-issuing blocked "
                                 "calls even with tools disabled")
                    final_answer = (msg.get("content") or "").strip() or (
                        "[Run stopped: the model kept re-issuing blocked tool "
                        "calls instead of answering]")
                    break

                # ---- Termination: no tool calls = final answer ----
                if not tool_calls:
                    # A generation cut at the completion cap DURING REASONING
                    # comes back finish_reason 'length' with no content at all
                    # (thinking ate the whole budget — seen live: tb-regex-log
                    # ended 'ok' with an empty answer after 8192 tokens of pure
                    # thinking). Nudge once for a brief direct reply instead of
                    # ending the run empty-handed.
                    if not (msg.get("content") or "").strip() \
                            and turn.get("finish_reason") == "length" \
                            and not cap_nudged:
                        cap_nudged = True
                        await emit("model_turn_capped", budget.iterations,
                                   {"model": eff_model,
                                    "completion_tokens":
                                        (turn.get("usage") or {})
                                        .get("completion_tokens")})
                        messages.append({"role": "user", "content":
                            "Your previous reply was cut off at the completion-"
                            "token cap during reasoning and contained no "
                            "answer. Reply now — briefly and directly, no "
                            "tool calls."})
                        continue
                    final_answer = msg.get("content") or ""
                    # Verifier gate: a text answer isn't "done" for a run that has a
                    # `verify` check — the check must pass. On failure, feed the report
                    # back and keep working (bounded by max_checks and the budget).
                    if verify_spec is not None and not verify_state["passed"]:
                        ok, report = await self._verify(verify_spec, verify_state, ctx, work_root)
                        verify_state["attempts"] += 1
                        await emit("verify", budget.iterations,
                                   {"ok": ok, "attempt": verify_state["attempts"],
                                    "command": verify_spec["command"], "report": report[:1500]})
                        if ok:
                            verify_state["passed"] = True
                            break
                        # #1 no-progress breaker: the same failure recurring means
                        # the agent isn't converging — stop early rather than burn
                        # the remaining checks/budget spinning on it.
                        sig = _verify_sig(report)
                        if sig == verify_stall["sig"]:
                            verify_stall["count"] += 1
                        else:
                            verify_stall["sig"], verify_stall["count"] = sig, 1
                        stuck = verify_stall["count"] >= stall_after
                        if stuck or verify_state["attempts"] >= verify_spec["max_checks"]:
                            status = "unverified"
                            why = (f"stuck on the same failure {verify_stall['count']}× "
                                   "(not converging)" if stuck else
                                   f"did not pass after {verify_state['attempts']} checks")
                            error_msg = f"verifier {why}: {verify_spec['command']}"
                            final_answer = ((final_answer or "").rstrip()
                                            + f"\n\n[NOT VERIFIED — {why}]\n{report}")
                            await emit("verify_giveup", budget.iterations,
                                       {"stuck": stuck, "attempts": verify_state["attempts"]})
                            break
                        messages.append({"role": "user", "content":
                            "The task is NOT complete — the verifier did not pass. Do NOT "
                            "modify the tests or the check; fix the real cause, then finish."
                            "\n\n" + report})
                        continue
                    break

                # ---- Execute tools ----
                # Gating (allowlist, parse, loop-guard, privacy, confirmation) is
                # ALWAYS sequential and stateful; only execution may be parallelized
                # (opt-in: runtime.parallel_tools.enabled). We first resolve each call
                # to either a precomputed result (rejected/declined) or an approved
                # (name,args) to run, then execute, then emit/append in the ORIGINAL
                # order so the transcript, trajectory and privacy taint stay consistent.
                # Expose the taint state on ctx so cloud-reaching TOOLS (council.
                # debate, eval.compare — see runtime/cloud_gate.py) can apply the
                # same privacy rule the loop applies to llm.call below.
                ctx.private_taint = bool(private_taint)
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
                    if allowed is not None and name not in allowed:
                        # The selected allowlist is a hard boundary, not just an
                        # exposure hint. Matters most for sub-agents — a research
                        # child literally cannot execute fs.write even if it tries.
                        plan["result"] = ToolResult(
                            status="error", result=None, tool_name=name,
                            error=f"tool '{name}' is not permitted in this run")
                        plans.append(plan)
                        continue
                    if (delegate_enforce and delegate_after and depth == 0
                            and not delegated
                            and inline_writes + 1 >= delegate_after
                            and name in _DELEGATE_GATE_TOOLS
                            and delegate_ok):
                        # Delegate gate, hard mode: this write would reach
                        # the threshold — reject it so the implementation
                        # goes through the specialist instead (after=1 blocks
                        # the very first inline write: delegate FIRST).
                        # Never a deadlock: one code.delegate call disarms.
                        plan["result"] = ToolResult(
                            status="error", result=None, tool_name=name,
                            error="inline implementation is closed for this "
                                  "run — call `code.delegate` with a "
                                  "complete, standalone task (the specialist "
                                  "model does the heavy lifting), then "
                                  "verify its report")
                        plans.append(plan)
                        continue
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError as e:
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
                    # Loop guard — exempt poll-safe tools (job.status/logs/wait):
                    # repeatedly checking the same job while it runs is expected.
                    # Repeats count only within the current mutation generation:
                    # a query repeated after any successful non-read_only call
                    # may see NEW state, so it is never a duplicate.
                    call_sig = self._call_signature(name, args)
                    poll_exempt = name in self._poll_safe
                    sig_key = (call_sig, mutation_gen)
                    if not poll_exempt and recent_calls.count(sig_key) >= 2:
                        guard_rejections += 1
                        # Escalation: enough refusals → the NEXT turn runs with
                        # tools disabled (the wrap-up is announced above the
                        # model-turn call). The refusal itself stays per-call.
                        if guard_max and guard_rejections >= guard_max:
                            wrap_up = True
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
                        recent_calls.append(sig_key)
                        if len(recent_calls) > 20:
                            recent_calls.pop(0)
                    # Near-duplicate guard (query-like tools only): the exact
                    # check above misses reworded repeats — the same search
                    # with shuffled/added words. Two similar calls are fine
                    # (refinement); the third is the overthinking pattern and
                    # is blocked with a synthesize-now message.
                    if not poll_exempt and name in near_dup_tools \
                            and near_dup_threshold:
                        ntok = self._arg_tokens(args)
                        similar = sum(
                            1 for pn, pgen, ptok in recent_query_calls
                            if pn == name and pgen == mutation_gen
                            and self._jaccard(ptok, ntok) >= near_dup_threshold)
                        if similar >= 2:
                            guard_rejections += 1
                            if guard_max and guard_rejections >= guard_max:
                                wrap_up = True
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
                        recent_query_calls.append((name, mutation_gen, ntok))
                        if len(recent_query_calls) > 20:
                            recent_query_calls.pop(0)
                    # Privacy gate: a cloud-LLM call while the conversation holds
                    # private tool results needs an explicit human ok — the request
                    # carries the full call args (the prompt), so the decision is
                    # informed. A refusal is a per-call error, never a run-ender:
                    # the model can fall back to a local tool. share_private is
                    # the blanket opt-in; auto_confirm deliberately does NOT
                    # waive this one.
                    if not share_private and private_taint and self._is_cloud_tool(name):
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
                        or (confirm_cloud and self._is_cloud_tool(name)))
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
                for plan in plans:
                    tc = plan["tc"]; name = plan["name"]
                    args = plan["args"]; result = plan["result"]
                    await emit("tool_result", budget.iterations, {
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
                    trajectory.append(_traj_entry(name, args, result))
                    tools_used.append(name)
                    # Loop guard invalidation: a successful call by anything not
                    # declared read_only may have changed what later calls return
                    # (files via code.run/agent.spawn/archives/..., stores via
                    # memory.append/rag.index, services via serve.*) — bump the
                    # mutation generation so re-queries after it are fresh, not
                    # duplicates. Pure queries and poll-safe probes bump nothing.
                    if result.status == "ok" and name not in self._poll_safe:
                        _tobj = self.registry.get(name)
                        if _tobj is not None and not getattr(_tobj, "read_only", False):
                            mutation_gen += 1
                    # #3 typed hand-off: remember files this run created/edited.
                    if (result.status == "ok" and name in _MUTATOR_TOOLS
                            and isinstance(args, dict)):
                        _p = (args.get("path") or args.get("to")
                              or args.get("dst") or args.get("file"))
                        if _p:
                            files_touched.add(str(_p))

                    # Update budget with tool's own LLM usage (llm.call,
                    # council/eval side calls, …)
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

                    # Crash/failure-loop escalation (execution tools only):
                    # N consecutive failures with the SAME signature earn a
                    # strategy-change hint — small brains otherwise retry the
                    # identical approach for hours (live: 70+ solver rebuilds).
                    fail_hint = ""
                    if fail_nudge_after and name in fail_nudge_tools:
                        failed, sig = _exec_failure(name, result)
                        if failed:
                            fail_count = fail_count + 1 if sig == fail_sig else 1
                            fail_sig = sig
                        else:
                            fail_sig, fail_count = None, 0
                        if failed and fail_count >= fail_nudge_after:
                            _del = (" Heavy implementation? `code.delegate` "
                                    "hands it to the specialist model — that "
                                    "is what it is for."
                                    if allowed is None or "code.delegate" in allowed
                                    else "")
                            fail_hint = (
                                f"\n\n[system note] {fail_count} consecutive "
                                "executions failed with the same error "
                                "signature. Do NOT retry the same approach "
                                "again — change strategy: simplify, switch "
                                "algorithm or language, verify on a tiny "
                                f"input first.{_del}")

                    # Delegate gate: count successful inline write/edit calls
                    # while code.delegate is available but unused. At the
                    # threshold, direct the brain to hand the implementation
                    # over; in enforce mode the 2x mark is the final warning
                    # (further inline edits are rejected pre-exec, above).
                    delegate_hint = ""
                    if name == "code.delegate":
                        delegated = True
                    if (delegate_after and depth == 0 and not delegated
                            and name in _DELEGATE_GATE_TOOLS
                            and result.status == "ok"
                            and delegate_ok):
                        inline_writes += 1
                        # Soft mode only: the directive rides the tool
                        # result. In enforce mode the threshold write is
                        # already rejected pre-exec (above) — the rejection
                        # IS the message.
                        if not delegate_enforce and inline_writes >= delegate_after:
                            delegate_hint = (
                                "\n\n[system note] You have made several "
                                "inline file edits — this is non-trivial "
                                "coding, which belongs with the "
                                "specialist. Call `code.delegate` with a "
                                "complete, standalone task (the heavy "
                                "transcript stays in the child's context, "
                                "not yours), then verify its report.")

                    # Append result to conversation
                    msg_idx = len(messages)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id") if isinstance(tc, dict) else None,
                        "name": name,
                        "content": (result.to_model_message()
                                    + fail_hint + delegate_hint),
                    })
                    if result.private:
                        private_taint.add(msg_idx)
                    # Image payload (e.g. browser.screenshot return_image): show
                    # it to the model as a follow-up user message with image
                    # blocks — only when the serving brain actually has vision.
                    if result.images and self.vision_enabled:
                        blocks = [{"type": "text",
                                   "text": f"Image output from {name}:"}]
                        blocks += [{"type": "image_url", "image_url": {"url": u}}
                                   for u in result.images]
                        messages.append({"role": "user", "content": blocks})

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
        except ModelTurnStalled as e:
            # The brain hung (no streamed output within budgets.stall_s) or a turn
            # ran past orchestrator.turn_timeout_s — end gracefully like
            # budget_exceeded: partial work and trajectory stay usable.
            status = "stalled"
            error_msg = str(e)
            log.warning("Run %s stalled: %s", run_id, e)
            final_answer = (
                f"[Run terminated: model stalled]\n"
                f"Partial result based on work so far: {final_answer or '(no answer produced yet)'}"
            )
        except Exception as e:
            status = "error"
            error_msg = f"{type(e).__name__}: {e}"
            log.exception("Unexpected error in agent loop")
            final_answer = f"[Internal error: {error_msg}]"

        summary = budget.summary()
        traj_str = _format_trajectory(trajectory)
        if subcall_server is not None:
            try:
                await subcall_server.close()
            except Exception:
                log.exception("subcall server close failed (continuing)")
        self.trace.finish_run(run_id, status, final_answer, error_msg, summary)
        _run_tmp_obj.cleanup()   # discard ephemeral per-run scratch
        await emit("run_finish", budget.iterations, {
            "status": status, "answer": final_answer,
            "error": error_msg or None, "budget": summary,
            "trajectory": traj_str,
            "guard_rejections": guard_rejections,
            "overthinking_markers": overthinking_markers,
            "prompt_tokens": first_prompt_tokens,
            "context_tokens": ctx_tokens or None,
        })

        return {
            "run_id": run_id,
            "status": status,
            "answer": final_answer,
            "error": error_msg or None,
            "budget": summary,
            "trajectory": traj_str,
            "guard_rejections": guard_rejections,
            "overthinking_markers": overthinking_markers,
            "verified": (None if verify_spec is None else verify_state["passed"]),
            "verify_command": (verify_spec["command"] if verify_spec else None),
            "files_changed": sorted(files_touched),
            "tools_used": tools_used,
        }

    async def _system_prompt(self, *, extra_system: str | None,
                             work_root: str | None, run_tmp: Path,
                             depth: int, eff_threshold: int,
                             run_overrides: dict) -> str:
        """Assemble the system prompt for a run.

        Everything in here is semi-static (base prompt, skill catalog,
        workspace, gate, specialist slot, location) so the whole system prefix
        — and the tool schemas the chat template renders with it — stays
        byte-identical across runs and the server prompt cache keeps hitting.
        The one per-run-varying fragment (current datetime) is NOT in here; it
        rides as a separate system message just before the user turn
        (_datetime_note), so only that line plus the user message re-prefills.
        """
        system_content = self.system_prompt
        if self.skill_catalog:
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
        # good at — so the brain doesn't blindly code.delegate to a research
        # model. Semi-static (live_slot is TTL-cached, and the line is stable
        # while the slot is unchanged), so it belongs in the cacheable system
        # prefix. Probe failure → omit the line entirely.
        try:
            from tools.model.catalog import live_slot as _live_slot
            _slot = await _live_slot(self.config)
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
            _reg = _sreg(self.config)
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

    def _is_cloud_tool(self, name: str) -> bool:
        """True if this tool reaches a remote/cloud LLM (privacy.remote_llm_tools)."""
        return name in (self.config.get("privacy", {}).get("remote_llm_tools", []) or [])

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
