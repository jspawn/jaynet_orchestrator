"""Tool base class and standard result envelope.

Every tool in /srv/orchestrator/tools/ subclasses `Tool` and is auto-discovered
at startup. Tools are async, declare their JSON schema, and return a normalized
`ToolResult` so the runtime can treat them uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ----------------------------------------------------------------------------
# Env scrubbing — model-influenced shell commands must not inherit the
# orchestrator's own secrets. Drop a small denylist of known secret names plus
# ANY var whose name ends in _KEY/_TOKEN/_SECRET/_PASSWORD; keep PATH, HOME,
# LANG and ordinary tooling vars. Deliberately simple and conservative. Shared
# by code.run and the verifier's check command (runtime/loop.py).
# ----------------------------------------------------------------------------
_SECRET_ENV_NAMES = {
    "LITELLM_MASTER_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TAVILY_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
}
_SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def scrub_env(env: dict) -> dict:
    """Return a copy of `env` with secrets stripped (rule above)."""
    return {k: v for k, v in env.items()
            if k not in _SECRET_ENV_NAMES
            and not k.upper().endswith(_SECRET_ENV_SUFFIXES)}


@dataclass
class ToolResult:
    """Normalized envelope every tool returns."""

    status: str                              # "ok" | "error"
    result: Any                              # tool-specific payload
    tool_name: str = ""
    error: str | None = None
    # Bookkeeping the runtime fills in:
    tokens_used: dict[str, int] = field(default_factory=dict)  # {prompt, completion, cached}
    cost_usd: float = 0.0
    latency_ms: int = 0
    # Privacy flag — propagated from the tool's own `private` declaration (the
    # single source of truth), so this result won't be forwarded to a remote LLM.
    private: bool = False
    # Image payloads as data URLs (data:image/png;base64,…). The loop shows them
    # to the model as image blocks when the serving brain has vision; otherwise
    # they are dropped. Deliberately NOT part of to_model_message (text-only).
    images: list[str] = field(default_factory=list)

    def to_model_message(self) -> str:
        """Serialize for the LLM. Truncates huge results to keep context lean."""
        import json
        if self.status == "error":
            return json.dumps({"status": "error", "error": self.error or "unknown"})
        # Keep payloads bounded — protects orchestrator context window.
        payload = self.result
        s = json.dumps({"status": "ok", "result": payload}, ensure_ascii=False, default=str)
        if len(s) > 20000:
            # Soft cap: tools should pre-summarize, but be defensive. Re-serialize
            # the head as a string field so the truncated message stays VALID JSON
            # (string-splicing the raw dump produced unparseable output).
            head = json.dumps(payload, ensure_ascii=False, default=str)[:20000]
            s = json.dumps({"status": "ok", "result": head + "…", "__truncated__": True},
                           ensure_ascii=False)
        return s


class Tool(ABC):
    """Base class for all tools. Subclass and place in /srv/orchestrator/tools/<namespace>/."""

    # Tool identity — must be unique. Convention: "<namespace>.<verb>"
    name: str = ""
    description: str = ""

    # JSON Schema for arguments (OpenAI tool-call format).
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    # Privacy: if True, results may not be passed to remote LLM tools
    # unless the request has share_private=True.
    private: bool = False

    # Confirmation: if True, runtime pauses for human approval before executing.
    requires_confirmation: bool = False

    # Poll-safe: idempotent status/wait tools (e.g. job.status, job.wait) that the
    # agent legitimately calls repeatedly with identical args while waiting on
    # work. These are exempt from the duplicate-call loop guard.
    poll_safe: bool = False

    # Read-only: a pure query — a successful call changes nothing that other
    # tools could observe (files, stores, services). Only successful calls by
    # NON-read-only tools bump the loop guard's mutation generation, which
    # invalidates earlier identical calls. Default False is the safe direction:
    # an unmarked tool merely invalidates more often; it can never make the
    # guard block a legitimate re-read after a change.
    read_only: bool = False

    def needs_confirmation(self, args: dict[str, Any], context: "ToolContext") -> bool:
        """Whether THIS call needs human approval. Defaults to the static
        `requires_confirmation` flag, but a tool may override to decide per-call
        from its args/config — e.g. code.run requires approval only when its
        sandbox is disabled. Keeping the default tied to the attribute means
        existing tools are unaffected."""
        return self.requires_confirmation

    @abstractmethod
    async def execute(self, args: dict[str, Any], context: "ToolContext") -> ToolResult:
        """Execute the tool. Must be async; may call external services."""
        raise NotImplementedError

    def to_openai_schema(self) -> dict[str, Any]:
        """Render as an OpenAI tool definition for the chat completion API."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolContext:
    """Per-request context passed to every tool.execute() call."""

    request_id: str
    config: dict[str, Any]                 # parsed runtime.yaml
    budget: "Budget"                       # forward ref; runtime.budget.Budget
    share_private: bool = False            # may private results leave the box?
    # Streaming hooks (set by the loop when a UI wants live output). A tool that
    # makes an LLM call (e.g. llm.call) may stream its tokens by awaiting
    # on_token(text, scope, model) for each delta. None on the CLI path.
    on_token: Any = None                   # async callable(text, scope, model)
    stream: bool = False
    # Whether the serving brain can consume image blocks (has a vision
    # projector). Set by the loop. Tools that can return images (e.g.
    # browser.screenshot return_image) should check this before attaching them.
    vision_enabled: bool = False
    # Sub-agent seam (set by the loop). A tool may launch a nested, bounded agent
    # run via `await ctx.spawn(task, ...)`; the loop owns the wiring (budget
    # carve-out, depth cap, confirmation routing). None when nesting isn't set up.
    spawn: Any = None                      # async callable(task, **kw) -> dict
    # Who is driving this run (web username, or None on the CLI/token path).
    # Used by tools that produce user-scoped artifacts (e.g. deliver.files).
    owner: Any = None
    # The agent's writable working directory for THIS run — the active project's
    # files dir, or (no project) a per-chat scratch dir. fs.* / code.* / archives
    # are confined here: this is the structural boundary, replacing any shared
    # global root. None only on the CLI path (falls back to tools.fs.allowed_roots).
    work_root: Any = None                  # str | Path | None
    # Ephemeral per-run scratch, auto-deleted when the run ends. For mid-run temp
    # files that shouldn't persist in the project/chat workspace.
    tmp_root: Any = None                   # str | Path | None
    # Emit a transport-neutral event to any live listener (the web stream),
    # `await ctx.emit(type, data)`. None on the CLI path. Tools use it sparingly,
    # e.g. to surface a download. The loop owns the wiring (trace + seq + sink).
    emit: Any = None                       # async callable(type: str, data: dict)
    # Human-question seam (set by the loop when a UI is attached). A tool may ask
    # the user structured questions and await their answers via
    # `await ctx.ask_user(questions) -> {qid: {value, text}}`. None on the CLI path.
    ask_user: Any = None                   # async callable(questions: list[dict]) -> dict | None
    # Progress-note seam (set by the loop). `note.set` calls ctx.set_note(text) to
    # write the agent's durable scratchpad, which the loop pins to every turn.
    set_note: Any = None                   # callable(text: str) -> None
    # Compaction-pin seam (set by the loop). `context.pin` calls ctx.pin_last() to
    # protect the most recent tool result from being stubbed by compaction.
    pin_last: Any = None                   # callable(reason: str) -> dict | None
    # Goal-declaration seam (set by the loop only when a /goal supervisor drives
    # this run). goal.complete / goal.blocked call ctx.goal_declare(status, text)
    # to record the verdict; the supervisor reads it after the run. None on any
    # ordinary run — the tools then tell the model there's no active goal.
    goal_declare: Any = None               # callable(status: str, text: str) -> None


# ----------------------------------------------------------------------------
# Workspace resolution — the SINGLE place every file tool resolves its root.
# fs.*, code.*, and archives all funnel through these so the work_root boundary
# is enforced uniformly (no per-tool copies, no shared global default).
# ----------------------------------------------------------------------------
def work_roots(ctx: "ToolContext") -> list[Path]:
    """Directories a file tool may touch in THIS run, in order: the run's
    work_root (project files dir, or a per-chat scratch dir) plus its ephemeral
    tmp_root. Falls back to tools.fs.allowed_roots ONLY when no work_root is set
    (the CLI path). There is no shared global default."""
    out: list[Path] = []
    for r in (getattr(ctx, "work_root", None), getattr(ctx, "tmp_root", None)):
        if r:
            out.append(Path(r).expanduser().resolve())
    if out:
        return out
    cfg = (ctx.config.get("tools", {}).get("fs", {}) or {})
    return [Path(r).expanduser().resolve() for r in (cfg.get("allowed_roots") or [])]


def resolve_in_roots(roots: list[Path], path: str, must_exist: bool = True) -> Path:
    """Resolve `path` and confine it to `roots`; raise if it escapes.

    A RELATIVE path is resolved against the workspace (the first root, i.e. the
    work_root), NOT the process CWD — so "notes.txt" means "<work_root>/notes.txt"
    and the agent can use bare relative paths exactly as the prompt tells it to.
    Absolute paths are taken as-is. Either way the result must land inside a root.
    """
    raw = Path(path).expanduser()
    if raw.is_absolute():
        p = raw.resolve()
    else:
        base = roots[0] if roots else Path.cwd()
        p = (base / raw).resolve()
    if not any(p == r or r in p.parents for r in roots):
        allowed = ", ".join(str(r) for r in roots) or "(no workspace configured)"
        raise PermissionError(
            f"{p} is outside your workspace. You can only read/write under: "
            f"{allowed}. Work there; read any path you've been given directly.")
    if must_exist and not p.exists():
        raise FileNotFoundError(f"no such file or directory: {p}{_not_found_hint(p)}")
    return p


def _not_found_hint(p: Path) -> str:
    """A short, actionable hint for a path that doesn't exist: name the first
    missing component and, if the directory that should contain it holds a
    case-/whitespace-similar name, suggest it (paths here are exact-match, so a
    space-vs-underscore or trailing-space difference is the usual culprit)."""
    try:
        missing = p
        while missing.parent != missing and not missing.parent.exists():
            missing = missing.parent          # walk up to the first existing ancestor
        parent = missing.parent
        if not (parent.exists() and parent.is_dir()):
            return ""
        names = [e.name for e in parent.iterdir()]
        match = _closest_name(missing.name, names)
        if match and match != missing.name:
            return (f" — no '{missing.name}' in '{parent}', but a similar name "
                    f"exists: '{match}'. Paths are case- and whitespace-sensitive; "
                    f"copy names exactly from fs.list.")
        return f" — '{missing.name}' does not exist in '{parent}' (list it to see exact names)"
    except Exception:
        return ""


def _closest_name(target: str, candidates: list[str]) -> str | None:
    """Best case-/whitespace-insensitive match for `target` among `candidates`."""
    import difflib
    import re

    def norm(s: str) -> str:
        # collapse runs of whitespace AND underscores to one space, fold case —
        # so '3_Custodian activities' == '3 Custodian activities' == '3  custodian_activities'
        return " ".join(re.split(r"[\s_]+", s.strip())).casefold()

    nt = norm(target)
    for c in candidates:
        if norm(c) == nt:                          # exact match after normalization
            return c
    m = difflib.get_close_matches(target, candidates, n=1, cutoff=0.8)
    return m[0] if m else None
