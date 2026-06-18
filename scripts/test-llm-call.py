#!/usr/bin/env python
"""Standalone smoke test for the llm.call tool against the live LiteLLM proxy.
Run on wolf with the proxy up and orchestrator.env sourced:

    set -a; source ~/.config/orchestrator.env; set +a
    /srv/orchestrator/.venv/bin/python /srv/orchestrator/scripts/test-llm-call.py
"""
import asyncio, sys
sys.path.insert(0, "/srv/orchestrator")

from runtime.tool_base import ToolContext
from runtime.budget import Budget
from tools.llm.cloud_models import CallCloudLLM


def _ctx():
    b = Budget(max_iterations=10, max_wall_clock_s=120, max_cost_usd=1.0,
               max_total_tokens=200000)
    return ToolContext(request_id="test", config={}, budget=b)


async def main():
    tool = CallCloudLLM()
    cases = [
        # (alias, task, think-override) — qwen_flash twice to show thinking off vs on
        ("haiku",        "Reply with exactly: ok", None),
        ("gemini_flash", "Reply with exactly: ok", None),
        ("qwen_flash",   "Reply with exactly: ok", None),    # default: thinking OFF
        ("qwen_flash",   "Reply with exactly: ok", True),    # force thinking ON
        ("claude",       "Name the capital of Switzerland in one word.", None),
        ("qwen_coder",   "Write a one-line Python lambda that squares x.", None),
    ]
    for alias, task, think in cases:
        args = {"model": alias, "task": task}
        if think is not None:
            args["think"] = think
        res = await tool.execute(args, _ctx())
        tag = f"{alias}" + (f" think={think}" if think is not None else "")
        if res.status == "ok":
            tok = res.tokens_used
            print(f"[OK]  {tag:22s} {res.latency_ms:5d}ms  "
                  f"comp={tok.get('completion',0):4d}  "
                  f"-> {res.result[:60]!r}")
        else:
            print(f"[ERR] {tag:22s} {res.error[:120]}")


if __name__ == "__main__":
    asyncio.run(main())
