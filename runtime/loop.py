"""Agent reasoning loop.

A bounded Level-1 agent: model proposes tool calls, runtime executes them,
results fed back, repeats until the model produces a final answer or any
budget ceiling is hit.

Key responsibilities:
- Translate between OpenAI tool-call format and our ToolResult envelope
- Enforce privacy: block private tool results from being passed to remote LLMs
- Detect repeat tool calls (same name+args 3× with no intervening write) → loop guard
  (and after loop_guard.max_rejections refusals, a tools-off wrap-up turn forces the answer)
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
import shutil
import tempfile
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
from .tool_base import Tool, ToolContext, ToolResult, scrub_env
from .trace import Trace

log = logging.getLogger(__name__)

# Qwen3-family brains wrap chain-of-thought in <think>…</think>. That reasoning
# must never reach the user's answer, the conversation history, or the trace as
# answer text — it belongs in the UI's collapsible "thinking" view (routed live
# via the "reasoning" token scope). These helpers strip/split it.
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


_SAMPLER_KEYS = ("temperature", "top_p", "top_k", "min_p", "repeat_penalty",
                 "presence_penalty", "frequency_penalty", "seed", "max_tokens")


class _NullAsyncCtx:
    """No-op async context manager — stand-in when a model call is ungated
    (cloud aliases, or a local backend with no configured concurrency limit)."""
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False


_NULL_ASYNC_CTX = _NullAsyncCtx()

# Default set of files a verifier owns and the agent must NOT edit to "pass":
# test modules + pytest conftest. Snapshotted before the run; a change = tampering.
_DEFAULT_VERIFY_PROTECT = ["**/test_*.py", "**/*_test.py", "**/tests/**/*.py", "**/conftest.py"]
# A "green" check that actually executed nothing — the classic way to fake a pass.
_VACUOUS_VERIFY_RE = re.compile(r"no tests ran|collected 0 items|=+ *0 passed", re.I)
# Tools whose success means a file was created/edited — surfaced as files_changed.
_MUTATOR_TOOLS = {"fs.write", "fs.edit", "fs.mkdir", "fs.move", "code.patch"}


def _verify_sig(report: str) -> str:
    """A stable fingerprint of a verifier failure, ignoring run-to-run noise
    (durations, counts, tmp paths, addresses). Same fingerprint twice => the
    agent is stuck on the identical failure, i.e. making no progress."""
    s = re.sub(r"/tmp/\S+|0x[0-9a-fA-F]+", "", report or "")
    s = re.sub(r"\d+", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:16]


def _sampler_body(sampling: dict | None) -> dict:
    """Whitelist sampler params for the /v1/chat/completions body, dropping
    None/unset keys. An empty/None input yields {} — i.e. send no sampler params,
    so the server falls back to the model's own (preset) defaults."""
    s = sampling or {}
    return {k: s[k] for k in _SAMPLER_KEYS if s.get(k) is not None}


def _child_budget(req: dict | None, db: dict | None, default_sub_iterations: int,
                  rem_cost: float, rem_tok: int, rem_wall: float) -> dict:
    """Assemble a spawned sub-agent's budget.

    Precedence per dimension: the spawn call's own `req` (budget arg) > config
    `db` (agent.default_budget) > the parent's REMAINING allowance (cost/tokens/
    wall) or `default_sub_iterations` (iterations). Cost/tokens/wall are clamped to
    the parent's remaining, so a child can never out-spend its parent; iterations
    are per-run and not clamped against the parent's remaining iterations.
    A rem_wall of 0 means the parent's wall-clock is DISABLED — the child then
    defaults to disabled too and any explicit wall is NOT clamped against it.
    """
    req = req or {}
    db = db or {}
    it = req.get("max_iterations", db.get("max_iterations", default_sub_iterations))
    wall = float(req.get("max_wall_clock_s", db.get("max_wall_clock_s", rem_wall)))
    if rem_wall:
        wall = min(wall, rem_wall)
    return {
        "max_cost_usd": min(float(req.get("max_cost_usd", db.get("max_cost_usd", rem_cost))), rem_cost),
        "max_total_tokens": min(int(req.get("max_total_tokens", db.get("max_total_tokens", rem_tok))), rem_tok),
        "max_iterations": int(it),
        "max_wall_clock_s": wall,
    }


def _is_local_model(model: str | None,
                    extra_local: frozenset = frozenset()) -> bool:
    """True for local llama.cpp aliases (local-orchestrator, local-coder, …).

    Only these honor `chat_template_kwargs` (the jinja thinking switch). Cloud
    providers reject unknown params — Anthropic 400s with "Extra inputs are not
    permitted" — so that key must never be sent to a cloud model. `extra_local`
    covers local aliases without the local- prefix — by convention the keys of
    orchestrator.local_concurrency (add a serve.start'd model there when it is
    registered under a custom alias).
    """
    return bool(model) and (model.startswith("local-") or model in extra_local)


def _turn_body(model: str, messages: list[dict], tools_schema: list[dict],
               sampling: dict | None, think: bool, stream: bool,
               extra_local: frozenset = frozenset()) -> dict:
    """Build the /v1/chat/completions body shared by both model-turn paths.

    `chat_template_kwargs` (the llama.cpp jinja thinking switch) is added ONLY for
    local models; cloud sub-agents run at the provider's default thinking mode
    (any reasoning is stripped from the answer downstream).
    """
    body: dict = {
        "model": model,
        "messages": messages,
        "tools": tools_schema,
        "tool_choice": "auto",
        **_sampler_body(sampling),
    }
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    if _is_local_model(model, extra_local):
        body["chat_template_kwargs"] = {"enable_thinking": think}
    return body


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


def _strip_think(text: str) -> str:
    """Remove complete <think>…</think> blocks from a finished string. Used on the
    non-streaming path and as a safety net on the assembled streaming content.
    An UNTERMINATED <think> (a truncated turn) is stripped to end-of-string too —
    otherwise raw chain-of-thought leaks into the answer."""
    if not text or _THINK_OPEN not in text:
        return text
    out = _THINK_RE.sub("", text)
    idx = out.find(_THINK_OPEN)
    if idx != -1:
        out = out[:idx]
    return out.strip()


def _suffix_prefix_len(s: str, tag: str) -> int:
    """Longest suffix of s that is a proper prefix of tag — i.e. how many trailing
    chars to hold back in case a tag is split across streamed chunks."""
    for k in range(min(len(s), len(tag) - 1), 0, -1):
        if s[-k:] == tag[:k]:
            return k
    return 0


def _traj_arg_hint(args: dict | None) -> str:
    """A short, non-sensitive hint of what a tool call was aimed at — taken from
    the call's *arguments* (the model's own inputs: a URL, query, path, model,
    collection), never from the result, so trajectory notes can't leak private
    tool output back into replayed history. `args` is None for calls rejected
    before parsing (allowlist / invalid-JSON gates) — hintless, not a crash."""
    for k in ("url", "query", "path", "task", "model", "collection", "name"):
        v = (args or {}).get(k)
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


class ModelTurnStalled(Exception):
    """A model turn hit its liveness bound: the stall watchdog saw no streamed
    output for budgets.stall_s (zombie backend), or the turn ran past the total
    orchestrator.turn_timeout_s cap. Raised from the model-turn paths; the loop
    ends the run gracefully as "stalled" — partial results and trajectory are
    preserved, exactly like the budget_exceeded path."""

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




class AgentRuntime:
    def __init__(self, config_path: str | Path | None = None):
        from runtime.paths import CONFIG
        self.config_path = Path(config_path) if config_path else CONFIG
        with self.config_path.open() as f:
            self.config = yaml.safe_load(f)

        orch_root = self.config_path.parent.parent
        tools_root = orch_root / "tools"
        self.registry = ToolRegistry(tools_root)
        self.registry.discover()
        # Idempotent status/wait tools exempt from the duplicate-call loop guard:
        # polling a job repeatedly with the same args is legitimate, not a loop.
        self._poll_safe = {t.name for t in self.registry.all()
                           if getattr(t, "poll_safe", False)}
        log.info("Discovered %d tools: %s",
                 len(self.registry.all()),
                 ", ".join(sorted(t.name for t in self.registry.all())))

        # Privacy is declared per-tool via each tool's own `private` flag — the
        # single source of truth (co-located with the tool that knows whether its
        # output is sensitive). Operators may OPTIONALLY force extra namespaces
        # private here without touching tool code; default is none.
        extra_private = set(self.config.get("privacy", {}).get("private_tool_namespaces", []) or [])
        for tool in self.registry.all():
            if tool.name.split(".", 1)[0] in extra_private:
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
        depth:     sub-agent nesting depth. 0 = top-level. Children spawned via
                   ctx.spawn run at depth+1, capped by config agent.max_depth.
        stream:    if True, the brain's model turns stream token-by-token (and
                   token/cost events are emitted). The CLI leaves this False to
                   keep the proven non-streaming path.
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
        # (e.g. the code.delegate coder) keeps its own server-preset sampling — the
        # brain's config defaults and per-run overrides never touch the coder.
        if eff_model == self.model:
            eff_sampling = {**(self.config["orchestrator"].get("sampling") or {}),
                            **(_ro.get("sampling") or {})}
            eff_sampling.setdefault("temperature", 0.7)   # brain fallback when config sets none
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
            guard_max = int((self.config.get("loop_guard") or {}).get("max_rejections", 6) or 0)
        except (TypeError, ValueError):
            guard_max = 6
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

        system_content = self.system_prompt
        if self.skill_catalog:
            system_content += "\n\n" + self.skill_catalog
        if extra_system:
            system_content += "\n\n" + extra_system
        if work_root:
            # Tell the model its workspace root up front, so it uses relative paths from
            # turn one instead of guessing an absolute install path (e.g. the live
            # /srv/orchestrator/… tree) and bouncing off the confinement wall on the
            # first fs.* call. work_root is stable within a project/chat, so this doesn't
            # disturb the cacheable prefix across runs in the same conversation.
            from runtime.paths import HOME as _ORCH_HOME
            system_content += (
                f"\n\n— Your workspace —\nYour files this run live under `{work_root}`. "
                f"For throwaway scripts/temp files use the scratch dir `{_run_tmp}` — NOT a "
                "bare `/tmp/...` path (that's outside your workspace and will be rejected). "
                "`fs.*` paths resolve relative to the workspace root — use RELATIVE paths; "
                "absolute paths outside these two roots are rejected. If you need the "
                "project's own source, it's in THIS workspace, not the live "
                f"`{_ORCH_HOME}/…` install tree. `fs.list .` / `fs.find` to orient first."
            )
        if depth == 0 and eff_threshold and 1 <= eff_threshold <= 4:
            system_content += (
                "\n\n— Complexity gate —\nBefore acting, rate this request's "
                "complexity 1-4 (1 = trivial, one obvious step · 2 = simple, a few "
                "tool calls · 3 = involved, multi-step · 4 = complex: multi-file, a "
                "real refactor, or an ambiguous design). If it is "
                f"{eff_threshold} or higher, call the `architect` tool with a "
                "complete, standalone task — it plans, has the coder poke holes, and "
                "executes in a fresh context — instead of diving in. Below "
                f"{eff_threshold}, just handle the request directly.")
        # Inject current datetime LAST so the model knows "now". It is the one
        # part of the system prefix that changes between runs (minute
        # resolution); everything before it (base prompt, skill catalog,
        # workspace, gate — and the tool schemas the chat template renders
        # right after the system message) then stays byte-identical across
        # runs, so the server prompt cache keeps hitting and only this short
        # tail plus the new user message re-prefills.
        from datetime import datetime as _dt, timezone as _tz
        import zoneinfo as _zi
        _tz_name = (self.config.get("orchestrator") or {}).get("timezone")
        if _tz_name:
            try:
                _now = _dt.now(_zi.ZoneInfo(_tz_name))
            except Exception:
                _now = _dt.now(_tz.utc).astimezone()
        else:
            _now = _dt.now(_tz.utc).astimezone()  # system timezone
        system_content += (
            f"\n\nCurrent date/time: {_now.strftime('%A, %Y-%m-%d %H:%M %Z')}"
        )
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
        mutation_gen = 0
        # Compact record of what this run did, folded into the answer so a
        # follow-up turn has the trajectory (not just the final text).
        trajectory: list[str] = []

        # Select tools ONCE, before the loop starts, and freeze the set for the
        # whole run. The tool schemas are a stable prefix; keeping them constant
        # is what preserves prompt-cache hits across iterations (see guide §3.7).
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
            config=self.config,
            budget=budget,
            share_private=share_private,
            on_token=(emit_token if stream else None),
            stream=stream,
            owner=owner,
            work_root=work_root,
            tmp_root=str(_run_tmp),
            vision_enabled=self.vision_enabled,
        )
        # Tool-facing event emitter (e.g. deliver.files surfacing a download).
        # Reuses the loop's emit so events get trace + seq + the live sink.
        async def tool_emit(etype: str, data: dict) -> None:
            await emit(etype, budget.iterations, data)
        ctx.emit = tool_emit

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
                        verify=None) -> dict:
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
            await emit("subagent_start", budget_obj.iterations, {
                "name": name or "sub-agent", "depth": depth + 1,
                "model": model or self.model, "tools": child_tools,
                "task": task[:500],
            })
            async def _child_progress(ev):
                # Surface a spawned agent's live steps in the parent's tool box: forward
                # each child event as a concise, typed progress line. Keeps the parent
                # card a live feed instead of a silent 'running…'.
                d = ev.get("data") or {}
                et = ev.get("type")
                if et == "tool_result":
                    mark = "\u2713" if d.get("status") == "ok" else "\u2717"
                    await emit("progress", budget_obj.iterations,
                               {"label": f"\u21b3 {d.get('tool', '?')} {mark}",
                                "type": "tool",
                                "ok": d.get("status") == "ok"})
                elif et == "model_turn":
                    # Forward child's commentary so the parent shows what it's doing
                    content = (d.get("content") or "").strip()
                    if content:
                        short = content[:150] + ("\u2026" if len(content) > 150 else "")
                        await emit("progress", budget_obj.iterations,
                                   {"label": f"\u21b3 {short}", "type": "prose"})
                elif et == "model_start":
                    await emit("progress", budget_obj.iterations,
                               {"label": "\u21b3 thinking\u2026", "type": "thinking"})
                elif et == "subagent_start":
                    await emit("progress", budget_obj.iterations,
                               {"label": f"\u21b3 spawn {d.get('name', 'sub-agent')}\u2026",
                                "type": "spawn"})
                elif et == "progress":
                    await emit("progress", budget_obj.iterations, d)   # bubble nested up
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
                owner=owner, work_root=work_root, think=think, stream=True,
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
        # Goal + progress anchor (fights goal-drift under compaction). The agent
        # keeps its note current via note.set → ctx.set_note; the loop restates the
        # goal and note on every turn (see _build_anchor).
        goal_text = user_message if isinstance(user_message, str) else ""
        progress = {"note": ""}
        ctx.set_note = lambda text: progress.__setitem__("note", (text or "")[:4000])
        # Working-anchor placement (off | system | trailing). Default off restores
        # the plain transcript — enable once you've confirmed your chat template
        # accepts the chosen placement. YAML `off` parses to False, so coerce.
        _am = (self.config.get("agent", {}).get("anchor", {}) or {}).get("mode", "off")
        anchor_mode = "off" if _am in (False, None, "off", "false", "") else str(_am).lower()
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
                _anchor = self._build_anchor(goal_text, progress["note"])
                call_messages = self._apply_anchor(messages, _anchor, anchor_mode)
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
                await emit("model_turn", budget.iterations, {
                    "model": eff_model,
                    "usage": turn.get("usage", {}),
                    "tool_calls": [
                        {"name": tc["function"]["name"], "args": tc["function"]["arguments"]}
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
                plans: list[dict] = []
                for tc in tool_calls:
                    name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"]
                    plan = {"tc": tc, "name": name, "args": None, "result": None}
                    if allowed is not None and name not in allowed:
                        # The selected allowlist is a hard boundary, not just an
                        # exposure hint. Matters most for sub-agents — a research
                        # child literally cannot execute fs.write even if it tries.
                        plan["result"] = ToolResult(
                            status="error", result=None, tool_name=name,
                            error=f"tool '{name}' is not permitted in this run")
                        plans.append(plan)
                        continue
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError as e:
                        plan["result"] = ToolResult(status="error", result=None, tool_name=name,
                                                    error=f"invalid JSON args: {e}")
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
        self.trace.finish_run(run_id, status, final_answer, error_msg, summary)
        _run_tmp_obj.cleanup()   # discard ephemeral per-run scratch
        await emit("run_finish", budget.iterations, {
            "status": status, "answer": final_answer,
            "error": error_msg or None, "budget": summary,
            "trajectory": traj_str,
            "guard_rejections": guard_rejections,
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
            "verified": (None if verify_spec is None else verify_state["passed"]),
            "verify_command": (verify_spec["command"] if verify_spec else None),
            "files_changed": sorted(files_touched),
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
            approved = cfg.get("non_interactive", "deny") == "allow"
            decision_src = f"non_interactive:{cfg.get('non_interactive', 'deny')}"
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
    def _build_anchor(goal: str, note: str):
        """A per-turn 'working anchor' the loop appends to every model call: the
        original goal restated + the agent's live progress note. Never persisted
        into the transcript (so it can't be compacted away) — rebuilt each turn."""
        goal = (goal or "").strip()
        if not goal:
            return None
        body = "— Working anchor (always current; not part of the transcript above) —\n"
        body += "GOAL: " + (goal if len(goal) <= 700 else goal[:700] + " …")
        if note:
            body += "\n\nYOUR PROGRESS NOTES (keep current with note.set):\n" + note
        body += ("\n\nStay on GOAL. If you catch yourself repeating a step that keeps "
                 "failing the same way, change approach or stop — don't spin.")
        return {"role": "system", "content": body}

    def _model_sem(self, model: str):
        """Concurrency gate (asyncio.Semaphore) for in-flight calls to `model`,
        or None if unbounded. Local backends map to their server's slot count;
        cloud aliases are unset → None → real off-box parallelism is unthrottled.
        The gate wraps a single call only (not the agent loop), so a parent that
        spawns children has already released its slot before awaiting them."""
        limit = self._local_concurrency.get(model)
        if not isinstance(limit, int) or limit <= 0:
            return None
        sem = self._model_sems.get(model)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            self._model_sems[model] = sem
        return sem

    def _normalize_verify(self, verify):
        """A verify arg — a command string, or {command, protect?, max_checks?,
        timeout_s?} — into a full spec, or None. Config agent.verify fills defaults."""
        if not verify:
            return None
        if isinstance(verify, str):
            verify = {"command": verify}
        if not isinstance(verify, dict):
            # e.g. verify=True — a truthy value with no command to run. There's nothing
            # to verify against, so treat it as "no verification" rather than crashing
            # on verify.get(). (Callers wanting verification must pass a command.)
            return None
        cmd = (verify.get("command") or "").strip()
        if not cmd:
            return None
        vcfg = (self.config.get("agent", {}) or {}).get("verify", {}) or {}
        return {
            "command": cmd,
            "protect": list(verify.get("protect") or vcfg.get("protect")
                            or _DEFAULT_VERIFY_PROTECT),
            "max_checks": int(verify.get("max_checks") or vcfg.get("max_checks", 4)),
            "timeout_s": int(verify.get("timeout_s") or vcfg.get("timeout_s", 180)),
        }

    @staticmethod
    def _snapshot_protected(work_root, patterns):
        """sha256 of every file matching the protect globs — the verifier's own
        code (tests/conftest) the agent must not rewrite to force a pass."""
        snap: dict[str, str] = {}
        if not work_root:
            return snap
        root = Path(work_root)
        for pat in patterns:
            try:
                for p in root.glob(pat):
                    if p.is_file():
                        snap[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
            except Exception:
                continue
        return snap

    async def _run_verify_command(self, command, cwd, timeout, ctx):
        """Run the check in the same posture as code.run (firejail, no network),
        confined to the work dir. Returns (exit_code, combined_output)."""
        cfg = (ctx.config.get("tools", {}).get("code", {}) or {}).get("run", {}) or {}
        prefix = cfg.get("sandbox_prefix")
        if prefix is None:
            prefix = ["firejail", "--quiet", "--private-tmp",
                      f"--whitelist={cwd}", "--read-only=/etc", "--net=none"]
        if prefix and not shutil.which(prefix[0]):
            prefix = []                       # sandbox binary missing → run bare
        # Scrub the orchestrator's secrets (same rule as code.run) — the check
        # command is model-influenced and its output goes back to the model.
        env = scrub_env(os.environ.copy())
        env.update({k: str(v) for k, v in (cfg.get("default_env") or {}).items()})
        argv = list(prefix) + ["bash", "-c", command]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
        except Exception as e:
            return 127, f"verifier could not start: {e}"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return 124, f"verifier timed out after {timeout}s"
        text = (out.decode("utf-8", "replace") + err.decode("utf-8", "replace")).strip()
        return proc.returncode, text

    async def _verify(self, spec, state, ctx, work_root):
        """Run the verifier once. Returns (passed, report). Fails on non-zero exit,
        a change to any protected test/check file (tampering), or a vacuous pass
        (exit 0 but zero tests executed)."""
        cwd = Path(work_root) if work_root else Path(".")
        code, out = await self._run_verify_command(spec["command"], cwd, spec["timeout_s"], ctx)
        tail = "\n".join((out or "").splitlines()[-40:])[-4000:]
        now = self._snapshot_protected(work_root, spec["protect"])
        base = state.get("baseline") or {}
        # Tampering = a baseline file MODIFIED or DELETED. A file newly CREATED
        # under the protect globs is not tampering — the delegate flow has the
        # agent write its own tests first, then implement against them.
        tampered = sorted(k for k in base if k not in now or now[k] != base[k])
        if tampered:
            return False, ("VERIFIER TAMPERING — the protected test/check files changed: "
                           f"{', '.join(tampered[:10])}. Revert them; make the real code "
                           "satisfy the existing tests, do not edit the tests.")
        if code == 0 and _VACUOUS_VERIFY_RE.search(out or ""):
            return False, ("The check exited 0 but executed NO tests — that is not a pass. "
                           f"Make the tests actually run.\n\n{tail}")
        if code == 0:
            return True, f"verifier passed: `{spec['command']}`"
        return False, f"verifier FAILED (exit {code}) — `{spec['command']}`:\n{tail}"

    def _turn_timeout_s(self) -> float:
        """Total cap for ONE model turn (orchestrator.turn_timeout_s, default 900).
        Local models generate ~40 tok/s, so a legitimately long turn needs many
        minutes — this is a hang backstop, not a pacing limit. 0 disables."""
        raw = (self.config.get("orchestrator") or {}).get("turn_timeout_s", 900)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 900.0

    def _stall_s(self) -> float:
        """Silence watchdog for STREAMING model turns (budgets.stall_s, default
        180): no SSE line at all for this long — or only keepalive/empty
        traffic with no content/tool-call delta — means the backend is hung
        (zombie). Applies to model turns ONLY — never during tool execution,
        where a long silent stretch is legitimate (a code.delegate child can
        run for many quiet minutes). 0 disables."""
        raw = (self.config.get("budgets") or {}).get("stall_s", 180)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 180.0

    def _http_client(self) -> httpx.AsyncClient:
        """The shared keep-alive client for model turns. Created lazily (tests
        monkeypatch httpx.AsyncClient, so construction must happen at call
        time, not in __init__) and reused across turns — one connection pool
        per runtime instead of a fresh TCP handshake per model turn."""
        c = getattr(self, "_http", None)
        if c is None:
            c = httpx.AsyncClient()
            self._http = c
        return c

    async def _model_turn(self, messages: list[dict], tools_schema: list[dict],
                          model: str | None = None, think: bool = True,
                          sampling: dict | None = None) -> dict:
        """One call to a model via LiteLLM (local brain or a cloud sub-agent)."""
        model = model or self.model
        body = _turn_body(model, messages, tools_schema, sampling, think,
                          stream=False, extra_local=self._local_aliases)
        timeout_s = self._turn_timeout_s()
        guard = self._model_sem(model) or _NULL_ASYNC_CTX
        try:
            async with guard:
                r = await self._http_client().post(
                    f"{self.litellm_base}/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer " + self._litellm_key()},
                    timeout=timeout_s or None,
                )
                if r.status_code >= 400:
                    # Surface the proxy's actual explanation instead of a bare code.
                    body = r.text[:1000]
                    log.error("model turn failed: HTTP %s from %s — %s",
                              r.status_code, model, body)
                    raise RuntimeError(f"LiteLLM {r.status_code} for model "
                                       f"'{model}': {body}")
                data = r.json()
                # A degenerate/empty completion (or a misbehaving backend — e.g. a
                # brain that returned nothing) can come back with no choices or a
                # null message. Coerce to a safe empty assistant turn so the loop
                # ends the run cleanly instead of crashing on message.get(...).
                _choices = data.get("choices") or []
                _msg = (_choices[0].get("message") if _choices else None) \
                    or {"role": "assistant", "content": None}
                return {"message": _msg, "usage": data.get("usage", {})}
        except httpx.TimeoutException:
            # No token heartbeat exists on this path — the total turn timeout is
            # its only liveness bound, so expiry means "stalled", not an error.
            raise ModelTurnStalled(
                f"model turn exceeded the {timeout_s:g}s total turn timeout "
                "(orchestrator.turn_timeout_s); ending the run with work so far "
                "preserved") from None

    async def complete(self, messages: list[dict], *, think: bool = False,
                       sampling: dict | None = None) -> dict:
        """One-shot, tool-free completion on the brain — for out-of-loop calls
        like /compact summarization. Returns {"content", "usage"}; any residual
        <think> block is stripped defensively (think is off, but a finetune can
        still emit one)."""
        r = await self._model_turn(messages, [], model=self.model, think=think,
                                   sampling=sampling)
        content = (r["message"].get("content") or "")
        content = re.sub(
            re.escape(_THINK_OPEN) + r".*?" + re.escape(_THINK_CLOSE),
            "", content, flags=re.S).strip()
        return {"content": content, "usage": r.get("usage") or {}}

    async def _model_turn_streaming(self, messages: list[dict],
                                    tools_schema: list[dict], on_token,
                                    model: str | None = None,
                                    think: bool = True,
                                    sampling: dict | None = None) -> dict:
        """Like _model_turn, but streams the response. Calls `await on_token(text)`
        for each content delta, assembles the streamed chunks back into the same
        {message, usage} shape the non-streaming path returns, and asks the proxy
        for usage via stream_options so cost still gets charged."""
        model = model or self.model
        body = _turn_body(model, messages, tools_schema, sampling, think,
                          stream=True, extra_local=self._local_aliases)
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

        stall_s = self._stall_s()
        timeout_s = self._turn_timeout_s()
        guard = self._model_sem(model) or _NULL_ASYNC_CTX
        async with guard:
            try:
                async with self._http_client().stream(
                    "POST", f"{self.litellm_base}/v1/chat/completions", json=body,
                    headers={"Authorization": "Bearer " + self._litellm_key()},
                    timeout=timeout_s or None,
                ) as r:
                    if r.status_code >= 400:
                        raw = await r.aread()
                        body_txt = raw.decode("utf-8", "replace")[:1000]
                        log.error("streaming model turn failed: HTTP %s — %s",
                                  r.status_code, body_txt)
                        raise RuntimeError(f"LiteLLM {r.status_code} for model "
                                           f"'{model}': {body_txt}")
                    # Stall watchdog (zombie detector), bounding MODEL TURNS
                    # only — it can never fire during tool execution, where
                    # a long silent stretch (e.g. a code.delegate child) is
                    # legitimate. Two liveness rules:
                    #  1. absolute silence — no SSE line at all within
                    #     stall_s (the wait_for below): a wedged backend;
                    #  2. payload silence — lines keep arriving (proxy
                    #     keepalives, role-only/empty chunks) but no
                    #     content or tool-call delta for stall_s: alive on
                    #     the wire, not generating. Keepalives deliberately
                    #     do NOT count as liveness, or a zombie behind a
                    #     chatty proxy would only surface at timeout_s.
                    # The whole turn is additionally capped at timeout_s.
                    lines = r.aiter_lines()
                    turn_started = time.monotonic()
                    last_payload = turn_started
                    while True:
                        try:
                            if stall_s > 0:
                                line = await asyncio.wait_for(anext(lines), timeout=stall_s)
                            else:
                                line = await anext(lines)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            raise ModelTurnStalled(
                                f"model '{model}' produced no streamed output for "
                                f"{stall_s:g}s (budgets.stall_s) — treating the hung "
                                "turn as stalled; work so far is preserved") from None
                        now = time.monotonic()
                        if 0 < timeout_s < now - turn_started:
                            raise ModelTurnStalled(
                                f"model turn exceeded the {timeout_s:g}s total turn "
                                "timeout (orchestrator.turn_timeout_s); work so far "
                                "is preserved")
                        if stall_s > 0 and now - last_payload > stall_s:
                            raise ModelTurnStalled(
                                f"model '{model}' streamed no completion content for "
                                f"{stall_s:g}s (budgets.stall_s) — only keepalive/"
                                "empty traffic; treating the hung turn as stalled, "
                                "work so far is preserved") from None
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
                        # Server-parsed chain-of-thought (llama.cpp splits the
                        # template-prefilled <think> block into reasoning_content;
                        # LiteLLM passes the field through). Route it to the UI's
                        # thinking view — and count it as liveness, or a long
                        # thinking stretch (empty content) would trip the stall
                        # watchdog's payload-silence rule.
                        rc = delta.get("reasoning_content")
                        if rc:
                            last_payload = now
                            if on_token:
                                await on_token(rc, "reasoning")
                        if delta.get("content"):
                            last_payload = now
                            await consume(delta["content"])
                        for tc in (delta.get("tool_calls") or []):
                            last_payload = now
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
            except httpx.TimeoutException:
                raise ModelTurnStalled(
                    f"model turn exceeded the {timeout_s:g}s total turn timeout "
                    "(orchestrator.turn_timeout_s); work so far is preserved") from None
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
        except asyncio.TimeoutError:
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
