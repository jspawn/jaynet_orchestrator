"""Tool base class and standard result envelope.

Every tool in /srv/orchestrator/tools/ subclasses `Tool` and is auto-discovered
at startup. Tools are async, declare their JSON schema, and return a normalized
`ToolResult` so the runtime can treat them uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


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
    # Privacy flag — set automatically by the dispatcher when the tool's
    # namespace is in `privacy.private_tool_namespaces`.
    private: bool = False

    def to_model_message(self) -> str:
        """Serialize for the LLM. Truncates huge results to keep context lean."""
        import json
        if self.status == "error":
            return json.dumps({"status": "error", "error": self.error or "unknown"})
        # Keep payloads bounded — protects orchestrator context window.
        payload = self.result
        s = json.dumps({"status": "ok", "result": payload}, ensure_ascii=False, default=str)
        if len(s) > 20000:
            # Soft cap: tools should pre-summarize, but be defensive.
            s = s[:20000] + '..."__truncated__":true}'
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
    trace_id: int | None = None            # current trace row, if logging
    # Streaming hooks (set by the loop when a UI wants live output). A tool that
    # makes an LLM call (e.g. llm.call) may stream its tokens by awaiting
    # on_token(text, scope, model) for each delta. None on the CLI path.
    on_token: Any = None                   # async callable(text, scope, model)
    stream: bool = False
    # Sub-agent seam (set by the loop). A tool may launch a nested, bounded agent
    # run via `await ctx.spawn(task, ...)`; the loop owns the wiring (budget
    # carve-out, depth cap, confirmation routing). None when nesting isn't set up.
    spawn: Any = None                      # async callable(task, **kw) -> dict
    # Who is driving this run (web username, or None on the CLI/token path).
    # Used by tools that produce user-scoped artifacts (e.g. deliver.files).
    owner: Any = None
    # Emit a transport-neutral event to any live listener (the web stream),
    # `await ctx.emit(type, data)`. None on the CLI path. Tools use it sparingly,
    # e.g. to surface a download. The loop owns the wiring (trace + seq + sink).
    emit: Any = None                       # async callable(type: str, data: dict)
    # Human-question seam (set by the loop when a UI is attached). A tool may ask
    # the user structured questions and await their answers via
    # `await ctx.ask_user(questions) -> {qid: {value, text}}`. None on the CLI path.
    ask_user: Any = None                   # async callable(questions: list[dict]) -> dict | None
