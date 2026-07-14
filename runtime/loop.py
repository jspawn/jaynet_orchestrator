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
    """
    req = req or {}
    db = db or {}
    it = req.get("max_iterations", db.get("max_iterations", default_sub_iterations))
    return {
        "max_cost_usd": min(float(req.get("max_cost_usd", db.get("max_cost_usd", rem_cost))), rem_cost),
        "max_total_tokens": min(int(req.get("max_total_tokens", db.get("max_total_tokens", rem_tok))), rem_tok),
        "max_iterations": int(it),
        "max_wall_clock_s": min(float(req.get("max_wall_clock_s", db.get("max_wall_clock_s", rem_wall))), rem_wall),
    }


def _is_local_model(model: str | None) -> bool:
    """True for local llama.cpp aliases (local-orchestrator, local-coder, …).

    Only these honor `chat_template_kwargs` (the jinja thinking switch). Cloud
    providers reject unknown params — Anthropic 400s with "Extra inputs are not
    permitted" — so that key must never be sent to a cloud model.
    """
    return bool(model) and model.startswith("local-")


def _turn_body(model: str, messages: list[dict], tools_schema: list[dict],
               sampling: dict | None, think: bool, stream: bool) -> dict:
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
    if _is_local_model(model):
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
    return compacted


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
                  grill: bool = False,
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
            eff_sampling.setdefault("temperature", 0.3)   # preserve the brain default
        else:
            eff_sampling = None
        _pt_cfg = self.config.get("parallel_tools")
        _pt_base = _pt_cfg if isinstance(_pt_cfg, dict) else {"enabled": bool(_pt_cfg)}
        eff_parallel = {**_pt_base, **(_ro.get("parallel_tools") or {})}
        warn_fraction = float(b_cfg.get("warn_fraction", 0.8) or 0)
        budget = Budget(
            max_iterations=b_cfg["max_iterations"],
            max_wall_clock_s=b_cfg["max_wall_clock_s"],
            max_cost_usd=b_cfg["max_cost_usd"],
            max_total_tokens=b_cfg["max_total_tokens"],
        )

        self.trace.start_run(run_id, user_message, owner=owner)

        # Ephemeral per-run scratch (ctx.tmp_root): mid-run temp files that must
        # not persist in the project/chat workspace. Deleted at run end. The
        # work_root (project files dir, or per-chat scratch) is passed in by the
        # caller; on the CLI it's None and file tools fall back to config.
        _run_tmp = Path(tempfile.mkdtemp(prefix=f"orchrun-{run_id[:8]}-"))

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
        # Inject current datetime so the model knows "now".
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
        if self.skill_catalog:
            system_content += "\n\n" + self.skill_catalog
        if extra_system:
            system_content += "\n\n" + extra_system
        if grill:
            system_content += (
                "\n\n\u2014 Grill me (clarify-first mode) \u2014\n"
                "The user has turned ON 'grill me': they would rather answer a question "
                "than have you guess. BEFORE doing substantive work, check the request for "
                "anything ambiguous, under-specified, or open to more than one reasonable "
                "reading \u2014 scope, target, format, missing inputs, edge cases \u2014 and if "
                "you find any, STOP and ask via ask.user (the fewest sharp questions that "
                "unblock you, 1\u20133) instead of assuming. Only proceed once the task is "
                "unambiguous. Don't interrogate over trivia you can safely infer, and don't "
                "ask more than needed \u2014 but when genuinely in doubt, ask rather than guess."
            )
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
        allowed = self.selector.select(user_message, requested=tools,
                                       disabled=disabled_tools)
        tools_schema = self.registry.openai_schemas(allowed)
        await emit("tool_selection", 0, {
            "mode": self.selector.mode,
            "requested": tools,
            "selected": allowed if allowed is not None else "all",
            "count": len(tools_schema),
            "diag": getattr(self.selector, "_diag", None),
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
            # Carve a sub-budget clamped to the parent's REMAINING allowance.
            pb = budget_obj
            req = budget or {}
            rem_cost = max(0.0, pb.max_cost_usd - pb.cost_usd)
            rem_tok = max(0, pb.max_total_tokens - pb.total_tokens)
            rem_wall = max(1.0, pb.max_wall_clock_s - pb.elapsed_s)
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
                auto_confirm=auto_confirm, on_event=_child_progress,
                confirm_provider=child_confirm, ask_provider=child_ask, model=model,
                depth=depth + 1, budget_overrides=child_overrides,
                owner=owner, work_root=work_root, think=think, stream=False,
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
            return child

        ctx.spawn = spawn

        final_answer = ""
        status = "ok"
        error_msg = ""
        budget_warned = False
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
                _comp_cfg = eff_compaction
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
                # ---- Model turn (streaming if a UI wants live tokens) ----
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
                        call_messages, tools_schema,
                        lambda t, scope="brain": emit_token(t, scope, eff_model),
                        model=eff_model, think=think, sampling=eff_sampling)
                else:
                    turn = await self._model_turn(call_messages, tools_schema,
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
                    call_sig = self._call_signature(name, args)
                    poll_exempt = name in self._poll_safe
                    if not poll_exempt and recent_calls.count(call_sig) >= 2:
                        plan["result"] = ToolResult(
                            status="error", result=None, tool_name=name,
                            error="duplicate tool call detected (loop guard); "
                                  "vary the arguments or stop calling this tool")
                        plans.append(plan)
                        continue
                    if not poll_exempt:
                        recent_calls.append(call_sig)
                        if len(recent_calls) > 20:
                            recent_calls.pop(0)
                    # Privacy gate
                    self._enforce_privacy(name, args, messages, private_taint, share_private)
                    # Confirmation gate: pause for human approval on tools that need
                    # it (job.start, git.commit, …) or that reach a cloud LLM when
                    # confirm_cloud_calls is on. No-op unless confirmation.enabled.
                    tool_obj = self.registry.get(name)
                    confirm_cloud = (self.config.get("confirmation", {}) or {}
                                     ).get("confirm_cloud_calls", True)
                    needs_confirm = (
                        (tool_obj is not None and tool_obj.needs_confirmation(args, ctx))
                        or (confirm_cloud and self._is_cloud_tool(name)))
                    if (needs_confirm
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
                    # #3 typed hand-off: remember files this run created/edited.
                    if (result.status == "ok" and name in _MUTATOR_TOOLS
                            and isinstance(args, dict)):
                        _p = (args.get("path") or args.get("to")
                              or args.get("dst") or args.get("file"))
                        if _p:
                            files_touched.add(str(_p))

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
        shutil.rmtree(_run_tmp, ignore_errors=True)   # discard ephemeral per-run scratch
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
        env = os.environ.copy()
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
        if now != base:
            changed = sorted((set(base) ^ set(now))
                             | {k for k in now if k in base and now[k] != base[k]})
            return False, ("VERIFIER TAMPERING — the protected test/check files changed: "
                           f"{', '.join(changed[:10])}. Revert them; make the real code "
                           "satisfy the existing tests, do not edit the tests.")
        if code == 0 and _VACUOUS_VERIFY_RE.search(out or ""):
            return False, ("The check exited 0 but executed NO tests — that is not a pass. "
                           f"Make the tests actually run.\n\n{tail}")
        if code == 0:
            return True, f"verifier passed: `{spec['command']}`"
        return False, f"verifier FAILED (exit {code}) — `{spec['command']}`:\n{tail}"

    async def _model_turn(self, messages: list[dict], tools_schema: list[dict],
                          model: str | None = None, think: bool = True,
                          sampling: dict | None = None) -> dict:
        """One call to a model via LiteLLM (local brain or a cloud sub-agent)."""
        model = model or self.model
        body = _turn_body(model, messages, tools_schema, sampling, think, stream=False)
        guard = self._model_sem(model) or _NULL_ASYNC_CTX
        async with guard:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(
                    f"{self.litellm_base}/v1/chat/completions",
                    json=body,
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
                # A degenerate/empty completion (or a misbehaving backend — e.g. a
                # brain that returned nothing) can come back with no choices or a
                # null message. Coerce to a safe empty assistant turn so the loop
                # ends the run cleanly instead of crashing on message.get(...).
                _choices = data.get("choices") or []
                _msg = (_choices[0].get("message") if _choices else None) \
                    or {"role": "assistant", "content": None}
                return {"message": _msg, "usage": data.get("usage", {})}

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
        body = _turn_body(model, messages, tools_schema, sampling, think, stream=True)
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

        guard = self._model_sem(model) or _NULL_ASYNC_CTX
        async with guard:
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
