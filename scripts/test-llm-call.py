#!/usr/bin/env python
"""Standalone smoke test for the llm.call tool against the live LiteLLM proxy.
Run with the proxy up and jaynet.env sourced:

    set -a; source ~/.config/jaynet.env; set +a
    $JAYNET_HOME/.venv/bin/python $JAYNET_HOME/scripts/test-llm-call.py
"""
import asyncio, os, sys
sys.path.insert(0, os.environ.get("JAYNET_HOME") or
                os.environ.get("ORCH_HOME", "/srv/orchestrator"))

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
        # (alias, task, think-override) — qwen twice to show thinking off vs on
        ("kimi",   "Reply with exactly: ok", None),
        ("qwen",   "Reply with exactly: ok", None),    # default: thinking OFF
        ("qwen",   "Reply with exactly: ok", True),    # force thinking ON
        ("gemini", "Name the capital of Switzerland in one word.", None),
        ("glm",    "Write a one-line Python lambda that squares x.", None),
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
