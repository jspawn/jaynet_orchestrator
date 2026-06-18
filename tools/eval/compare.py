"""eval.compare — run one prompt across several models and compare.

Fires the same prompt at N models concurrently through the LiteLLM proxy and
returns their outputs side by side with latency, token usage and per-model cost.
Useful for "which model should I use for X", regression-checking a prompt, or
sanity-checking the local orchestrator against a frontier model.

Models may be given as llm.call aliases (haiku, claude, qwen_max, gemini_flash,
...), as the literal LiteLLM model_name (claude-sonnet, gemini-flash, ...), or
'local'/'local-orchestrator' for the local brain.

Spend is charged to the run's Budget per sub-call (using runtime.yaml `costs`),
so a comparison across pricey models still counts against max_cost_usd.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx

from runtime.tool_base import Tool, ToolContext, ToolResult

# Reuse llm.call's alias map if available; otherwise fall back to identity.
try:
    from tools.llm.cloud_models import _MODEL_MAP as _ALIASES
except Exception:  # pragma: no cover - cloud_models may be absent in tests
    _ALIASES = {}

_LOCAL_ALIASES = {"local", "local-orchestrator", "orchestrator"}


def _resolve(model: str, ctx: ToolContext) -> str:
    """alias -> litellm model_name; pass through raw names and the local model."""
    if model in _LOCAL_ALIASES:
        return ctx.config.get("orchestrator", {}).get("model", "local-orchestrator")
    return _ALIASES.get(model, model)


def _litellm_base(ctx: ToolContext) -> str:
    return ctx.config.get("orchestrator", {}).get("litellm_base", "http://127.0.0.1:4000")


def _cost(model_name: str, prompt: int, completion: int, cached: int,
          cost_table: dict) -> float:
    rates = cost_table.get(model_name)
    if not rates:
        return 0.0
    billable_prompt = max(0, prompt - cached) + cached * 0.1
    return (billable_prompt * rates.get("input", 0)
            + completion * rates.get("output", 0)) / 1_000_000


async def _one(client: httpx.AsyncClient, model_in: str, messages: list[dict],
               temperature: float, max_tokens: int | None, want_json: bool,
               ctx: ToolContext) -> dict:
    model_name = _resolve(model_in, ctx)
    body: dict = {"model": model_name, "messages": messages, "temperature": temperature}
    if max_tokens:
        body["max_tokens"] = max_tokens
    if want_json:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": "Bearer " + os.environ.get("LITELLM_MASTER_KEY", "")}
    start = time.monotonic()
    try:
        r = await client.post(f"{_litellm_base(ctx)}/v1/chat/completions",
                              json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        return {"model": model_in, "model_name": model_name, "status": "error",
                "error": f"HTTP {e.response.status_code}: {e.response.text[:300]}",
                "latency_ms": int((time.monotonic() - start) * 1000)}
    except Exception as e:
        return {"model": model_in, "model_name": model_name, "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "latency_ms": int((time.monotonic() - start) * 1000)}

    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    usage = data.get("usage", {}) or {}
    ptd = usage.get("prompt_tokens_details")
    cached = ptd.get("cached_tokens", 0) if isinstance(ptd, dict) else 0
    prompt_t = usage.get("prompt_tokens", 0)
    completion_t = usage.get("completion_tokens", 0)
    cost = _cost(model_name, prompt_t, completion_t, cached, ctx.config.get("costs", {}))

    # Charge the run budget directly (multi-model spend the loop can't see via
    # the single tokens_used envelope). Accumulates now; the next loop tick()
    # enforces any ceiling.
    try:
        ctx.budget.add_usage(model_name, prompt=prompt_t, completion=completion_t,
                             cached=cached, cost_table=ctx.config.get("costs", {}))
    except Exception:
        pass

    truncated = len(content) > 2000
    return {
        "model": model_in,
        "model_name": model_name,
        "status": "ok",
        "output": content[:2000] + ("…" if truncated else ""),
        "output_truncated": truncated,
        "output_chars": len(content),
        "latency_ms": int((time.monotonic() - start) * 1000),
        "tokens": {"prompt": prompt_t, "completion": completion_t, "cached": cached},
        "cost_usd": round(cost, 6),
    }


class EvalCompare(Tool):
    name = "eval.compare"
    description = (
        "Run the same prompt across several models concurrently and compare their "
        "outputs, latency, tokens and cost. Use to decide which model fits a task "
        "or to check the local model against a frontier one. Models: llm.call "
        "aliases (haiku, claude, qwen_max, gemini_flash...), raw LiteLLM names, or "
        "'local' for the orchestrator brain."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The user prompt sent to every model."},
            "models": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                       "description": "Model aliases or names to compare."},
            "system": {"type": "string", "description": "Optional shared system prompt."},
            "temperature": {"type": "number", "default": 0.3, "minimum": 0, "maximum": 2},
            "max_tokens": {"type": "integer", "minimum": 1,
                           "description": "Optional cap on each model's output."},
            "json": {"type": "boolean", "default": False,
                     "description": "Request a JSON object response from each model."},
        },
        "required": ["prompt", "models"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        models = args["models"]
        if not models:
            return ToolResult(status="error", result=None, error="no models given")
        messages = []
        if args.get("system"):
            messages.append({"role": "system", "content": args["system"]})
        messages.append({"role": "user", "content": args["prompt"]})

        temperature = float(args.get("temperature", 0.3))
        max_tokens = args.get("max_tokens")
        want_json = bool(args.get("json"))

        async with httpx.AsyncClient(timeout=180) as client:
            results = await asyncio.gather(*[
                _one(client, m, messages, temperature, max_tokens, want_json, ctx)
                for m in models
            ])

        ok = [r for r in results if r["status"] == "ok"]
        summary = {
            "total_cost_usd": round(sum(r.get("cost_usd", 0) for r in ok), 6),
            "fastest": min(ok, key=lambda r: r["latency_ms"])["model"] if ok else None,
            "cheapest": min(ok, key=lambda r: r["cost_usd"])["model"] if ok else None,
            "succeeded": len(ok),
            "failed": len(results) - len(ok),
        }
        # cost_usd on the envelope is informational; budget was already charged.
        total = summary["total_cost_usd"]
        return ToolResult(status="ok", result={"results": results, "summary": summary},
                          cost_usd=total)
