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


from runtime.paths import LITELLM_BASE as _LITELLM_BASE

# alias -> litellm.yaml model_name. Four external models, pick by need.
# These are the DEFAULTS — the live map comes from the cloud_models DB table
# (admin → Presets → Cloud models), layered via set_active() on startup and
# after every admin edit.
_DEFAULT_MODEL_MAP = {
    # preferred frontier (reasoning, coding, long-doc; 1M ctx, always-on thinking)
    "kimi":    "kimi-k3",
    # coding assistance (strong open-source coder, 1M context)
    "glm":     "glm-5.2",
    # reasoning (strong, mid-cost)
    "gemini":  "gemini-pro",
    # cheap cloud checks (fast, cheap)
    "qwen":    "qwen-plus",
}

_DEFAULT_ROLES = {
    "kimi":   "Kimi K3 (Moonshot), frontier MoE, 1M context, always-on "
              "reasoning — the default for anything non-trivial.",
    "glm":    "GLM 5.2, alternate coder + 1M context.",
    "gemini": "Gemini 3.5, alternate reasoner / second opinion.",
    "qwen":   "Qwen 3.6 Plus, cheap/fast bulk checks.",
}

# Aliases where we force thinking OFF by default.
_DEFAULT_THINKING_OFF = {"qwen-plus"}

# The live, DB-layered view: {friendly: {litellm_alias, thinking, role, ...}}.
_ACTIVE: dict = {}


def set_active(cloud: dict) -> None:
    """Swap the active cloud catalog (called by cloud_store.load_into_config)."""
    global _ACTIVE
    _ACTIVE = dict(cloud or {})


def _maps(config: dict | None = None):
    """(friendly->litellm map, thinking-off set, roles) — DB-layered config
    wins, module defaults otherwise."""
    cloud = ((config or {}).get("models") or {}).get("cloud") or _ACTIVE
    if not cloud:
        return (dict(_DEFAULT_MODEL_MAP), set(_DEFAULT_THINKING_OFF),
                dict(_DEFAULT_ROLES))
    m = {k: v["litellm_alias"] for k, v in cloud.items()}
    off = {v["litellm_alias"] for k, v in cloud.items()
           if v.get("thinking") == "off"}
    roles = {k: v.get("role") or "" for k, v in cloud.items()}
    return m, off, roles


def resolve_model_alias(name: str | None, config: dict | None = None) -> str | None:
    """Normalize a model name to a litellm.yaml model_name.

    Accepts a friendly alias (glm, gemini, qwen) OR a real litellm alias
    (glm-5.2, gemini-pro, qwen-plus, local-specialist, …) and returns the
    litellm alias. Tolerant of case and _/- differences. Returns None if the
    name matches nothing.
    """
    if not name:
        return None
    model_map, _, _ = _maps(config)
    litellm_aliases = set(model_map.values()) | {"local-orchestrator",
                                                 "local-specialist"}
    if name in model_map:
        return model_map[name]
    if name in litellm_aliases:
        return name
    norm = name.strip().lower().replace("_", "-")
    if norm in litellm_aliases:
        return norm
    for k, v in model_map.items():
        if k.replace("_", "-") == norm:
            return v
    return None


def valid_model_names(config: dict | None = None) -> list[str]:
    """Everything a caller may pass: friendly aliases + real litellm aliases."""
    model_map, _, _ = _maps(config)
    return sorted(set(model_map) | set(model_map.values())
                  | {"local-orchestrator", "local-specialist"})


async def _call_via_litellm(alias: str, task: str, payload: str | None,
                            system: str | None, want_json: bool,
                            think: bool | None, ctx: ToolContext) -> ToolResult:
    """Shared implementation. Returns a ToolResult carrying content + token usage."""
    _, thinking_off, _ = _maps(ctx.config)
    model = resolve_model_alias(alias, ctx.config)
    if model is None:
        return ToolResult(status="error", result=None,
                          error=f"unknown model alias '{alias}'. "
                                f"valid: {', '.join(valid_model_names(ctx.config))}")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    user_content = task if not payload else f"{task}\n\n---\n\n{payload}"
    messages.append({"role": "user", "content": user_content})

    body: dict = {"model": model, "messages": messages, "temperature": 0.3}

    # Emit progress so the UI shows what's being sent to the cloud model.
    _emit = getattr(ctx, "emit", None)
    if _emit:
        prompt_preview = user_content[:300] + ("…" if len(user_content) > 300 else "")
        try:
            await _emit("progress", {
                "label": f"→ {model}" + (f" (system: {system[:80]}…)" if system and len(system) > 80 else f" (system: {system})" if system else ""),
                "type": "stage"})
            await _emit("progress", {
                "label": f"prompt: {prompt_preview}",
                "type": "prose"})
        except Exception:
            pass

    if want_json:
        body["response_format"] = {"type": "json_object"}

    # Resolve thinking: explicit `think` wins; else default per (resolved) alias.
    if think is None:
        think = model not in thinking_off
    if not think:
        # DashScope (Qwen) honours enable_thinking; Gemini honours a 0 budget.
        # LiteLLM forwards unknown keys to the provider; drop_params strips any
        # a given backend rejects, so this is safe to send broadly.
        if model.startswith("qwen"):
            body["extra_body"] = {"enable_thinking": False}
        elif model.startswith("gemini"):
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

    # Emit completion stats so the activity feed shows what happened.
    if _emit:
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        ms = int((time.monotonic() - start) * 1000)
        try:
            await _emit("progress", {
                "label": f"← {model}: {pt} in / {ct} out / {ms}ms" + (f" ({cached} cached)" if cached else ""),
                "type": "tool", "ok": True})
        except Exception:
            pass

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
    read_only = True
    description = (
        "Delegate a self-contained task to a cloud LLM. Pick `model` by "
        "cost/capability. Pass a complete, standalone task — no conversation "
        "history is shared."
    )

    @property
    def parameters(self) -> dict:
        # Built from the ACTIVE cloud catalog (DB-layered via set_active), so
        # admin edits to the model list apply without a code change.
        model_map, _, roles = _maps(None)
        enum = sorted(model_map)
        model_desc = " ".join(
            f"{k}: {roles.get(k) or model_map[k]}" for k in enum)
        return {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "enum": enum,
                    "description": model_desc,
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
                        "the per-model default (off for cheap tiers, on "
                        "otherwise; always-on-reasoning models ignore this). "
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
