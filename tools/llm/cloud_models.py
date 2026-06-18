"""Cloud LLM tools — a single consolidated `llm.call` tool.

The orchestrator delegates self-contained subtasks to a cloud model by calling
`llm.call` with a `model` alias chosen by cost/capability. One tool with a
`model` enum (rather than N near-identical tools) keeps the tool-schema prefix
small and cache-friendly — see guide §3.7.

Token-efficiency principles:
- Each call builds a SELF-CONTAINED prompt from `task` (+ optional `payload`).
  No conversation history is forwarded — the orchestrator owns that.
- Optional `system` override and `format: "json"` per call.
- Thinking/reasoning models (Qwen3.5, Gemini) emit a chain-of-thought that we
  do NOT forward to the orchestrator: we read only `choices[0].message.content`.
  For the cheap/fast tier we also DISABLE thinking at the provider so a trivial
  task doesn't burn hundreds of reasoning tokens.
"""

from __future__ import annotations

import os
import time
import json
import httpx

from runtime.tool_base import Tool, ToolContext, ToolResult


_LITELLM_BASE = "http://127.0.0.1:4000"

# alias -> litellm.yaml model_name. Only Claude + Gemini + Qwen are wired up.
_MODEL_MAP = {
    # cheap / fast
    "haiku":        "claude-haiku",
    "gemini_flash": "gemini-flash",
    "qwen_flash":   "qwen-flash",
    # workhorse reasoning / writing
    "claude":       "claude-sonnet",
    "qwen_plus":    "qwen-plus",
    # frontier (costly) — use sparingly
    "opus":         "claude-opus",
    "qwen_max":     "qwen-max",
    # code specialist
    "qwen_coder":   "qwen-coder",
    # long context
    "gemini_pro":   "gemini-pro",
}

# Aliases where we force thinking OFF by default: the fast tier (don't pay for a
# chain-of-thought on cheap tasks) and the code specialist (wants direct output).
# The orchestrator can still override per call via the `think` argument.
_THINKING_OFF_BY_DEFAULT = {"qwen_flash", "qwen_plus", "qwen_coder", "gemini_flash"}


async def _call_via_litellm(alias: str, task: str, payload: str | None,
                            system: str | None, want_json: bool,
                            think: bool | None, ctx: ToolContext) -> ToolResult:
    """Shared implementation. Returns a ToolResult carrying content + token usage."""
    model = _MODEL_MAP.get(alias)
    if model is None:
        return ToolResult(status="error", result=None,
                          error=f"unknown model alias '{alias}'. "
                                f"valid: {', '.join(sorted(_MODEL_MAP))}")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    user_content = task if not payload else f"{task}\n\n---\n\n{payload}"
    messages.append({"role": "user", "content": user_content})

    body: dict = {"model": model, "messages": messages, "temperature": 0.3}
    if want_json:
        body["response_format"] = {"type": "json_object"}

    # Resolve thinking: explicit `think` wins; else default per alias.
    if think is None:
        think = alias not in _THINKING_OFF_BY_DEFAULT
    if not think:
        # DashScope (Qwen) honours enable_thinking; Gemini honours a 0 budget.
        # LiteLLM forwards unknown keys to the provider; drop_params strips any
        # a given backend rejects, so this is safe to send broadly.
        if alias.startswith("qwen"):
            body["extra_body"] = {"enable_thinking": False}
        elif alias.startswith("gemini"):
            body["reasoning_effort"] = "none"

    headers = {"Authorization": "Bearer " + os.environ.get("LITELLM_MASTER_KEY", "")}
    start = time.monotonic()
    on_token = getattr(ctx, "on_token", None)

    if on_token:
        # Streaming path: forward the cloud model's tokens to the UI live, then
        # assemble the same content + usage the non-streaming path produces.
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        parts: list[str] = []
        usage: dict = {}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("POST", f"{_LITELLM_BASE}/v1/chat/completions",
                                         json=body, headers=headers) as r:
                    if r.status_code >= 400:
                        raw = await r.aread()
                        return ToolResult(status="error", result=None,
                                          error=f"HTTP {r.status_code}: "
                                                f"{raw.decode('utf-8','replace')[:500]}",
                                          latency_ms=int((time.monotonic()-start)*1000))
                    async for line in r.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        p = line[5:].strip()
                        if p == "[DONE]":
                            break
                        try:
                            chunk = json.loads(p)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = (choices[0].get("delta") or {}).get("content")
                        if delta:
                            parts.append(delta)
                            await on_token(delta, "llm.call", model)
        except Exception as e:
            return ToolResult(status="error", result=None,
                              error=f"{type(e).__name__}: {e}",
                              latency_ms=int((time.monotonic() - start) * 1000))
        content = "".join(parts)
    else:
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(f"{_LITELLM_BASE}/v1/chat/completions",
                                      json=body, headers=headers)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPStatusError as e:
            return ToolResult(status="error", result=None,
                              error=f"HTTP {e.response.status_code}: {e.response.text[:500]}",
                              latency_ms=int((time.monotonic() - start) * 1000))
        except Exception as e:
            return ToolResult(status="error", result=None,
                              error=f"{type(e).__name__}: {e}",
                              latency_ms=int((time.monotonic() - start) * 1000))
        # Extract ONLY the final content — never the reasoning/thinking blocks.
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        usage = data.get("usage", {})

    cached = 0
    ptd = usage.get("prompt_tokens_details")
    if isinstance(ptd, dict):
        cached = ptd.get("cached_tokens", 0)

    return ToolResult(
        status="ok",
        result=content,
        tokens_used={
            "model": model,
            "prompt": usage.get("prompt_tokens", 0),
            "completion": usage.get("completion_tokens", 0),
            "cached": cached,
        },
        latency_ms=int((time.monotonic() - start) * 1000),
    )


class CallCloudLLM(Tool):
    name = "llm.call"
    description = (
        "Delegate a self-contained task to a cloud LLM. Pick `model` by "
        "cost/capability; default to the cheapest tier that can do the job. "
        "Pass a complete, standalone task — no conversation history is shared."
    )
    parameters = {
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "enum": ["haiku", "gemini_flash", "qwen_flash",
                         "claude", "qwen_plus",
                         "opus", "qwen_max",
                         "qwen_coder", "gemini_pro"],
                "description": (
                    "cheap/fast: haiku, gemini_flash, qwen_flash. "
                    "workhorse reasoning/writing: claude, qwen_plus. "
                    "frontier (costly, use sparingly): opus, qwen_max. "
                    "code: qwen_coder. long-context: gemini_pro."),
            },
            "task": {
                "type": "string",
                "description": "What to do. Specific and self-contained.",
            },
            "payload": {
                "type": "string",
                "description": "Optional content to act on (text to summarize, code, etc.).",
            },
            "system": {
                "type": "string",
                "description": "Optional system prompt override.",
            },
            "format": {
                "type": "string",
                "enum": ["text", "json"],
                "description": "Output format. 'json' requests a JSON object.",
            },
            "think": {
                "type": "boolean",
                "description": (
                    "Force the model's thinking/reasoning on or off. Omit to use "
                    "the per-model default (off for fast/code tiers, on otherwise). "
                    "Turn on for hard reasoning; off to save tokens/latency."),
            },
        },
        "required": ["model", "task"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        return await _call_via_litellm(
            args["model"], args["task"], args.get("payload"),
            args.get("system"), args.get("format") == "json",
            args.get("think"), ctx,
        )
