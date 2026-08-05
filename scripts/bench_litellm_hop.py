#!/usr/bin/env python
"""Benchmark: LiteLLM proxy hop vs direct-to-llama for the local brain.

Sends identical streaming chat completions to the brain DIRECTLY (:8090) and
through the LiteLLM proxy (:4000, alias local-orchestrator) at several prompt
sizes, and reports TTFT / total / tok-s so we can see what the proxy hop costs
as a function of prompt size. Thinking is disabled and completions are capped
short — the question is transport+prefill overhead, not generation.

Every call carries a unique nonce: LiteLLM has response caching enabled
(ttl 600s) and llama-server has a prompt cache — both would fake the numbers.

Run:  set -a; source ~/.config/orchestrator.env; set +a
      $ORCH_HOME/.venv/bin/python $ORCH_HOME/scripts/bench_litellm_hop.py
"""
import asyncio, os, time, uuid

import httpx

DIRECT = "http://127.0.0.1:8090/v1/chat/completions"
PROXY = "http://127.0.0.1:4000/v1/chat/completions"
KEY = os.environ.get("LITELLM_MASTER_KEY", "")
# rough prompt budgets in tokens (approximated by chars/4)
SIZES = [512, 8_000, 32_000, 64_000]
MAX_TOKENS = 16

FILLER = ("The quick brown fox jumps over the lazy dog near the river bank while "
          "a gentle breeze moves through the tall grass and the afternoon sun "
          "casts long shadows across the quiet meadow beyond the old stone wall. ")


def _messages(approx_tokens: int, nonce: str):
    body = (FILLER * ((approx_tokens * 4 // len(FILLER)) + 1))[: approx_tokens * 4]
    return [{"role": "user", "content": f"[run {nonce}] Summarize in one word.\n\n{body}"}]


async def _one(client: httpx.AsyncClient, url: str, model: str, messages, auth: bool):
    body = {"model": model, "messages": messages, "stream": True,
            "max_tokens": MAX_TOKENS, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
            "stream_options": {"include_usage": True}}
    headers = {"Authorization": f"Bearer {KEY}"} if auth else {}
    ttft = total = None
    usage = {}
    t0 = time.monotonic()
    async with client.stream("POST", url, json=body, headers=headers,
                             timeout=httpx.Timeout(600.0, connect=5.0)) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if p == "[DONE]":
                break
            if ttft is None:
                ttft = time.monotonic() - t0
            try:
                import json
                chunk = json.loads(p)
            except Exception:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
    total = time.monotonic() - t0
    return ttft, total, usage


async def main():
    print(f"{'size':>7} {'path':>7} {'TTFT_s':>8} {'total_s':>8} {'prefill_tok':>11} {'hop_ms':>8}")
    async with httpx.AsyncClient() as client:
        # baseline: tiny request both ways, measures fixed proxy overhead
        for size in SIZES:
            row = {}
            for label, url, model, auth in (
                    ("direct", DIRECT, "whatever", False),
                    ("proxy", PROXY, "local-orchestrator", True)):
                nonce = uuid.uuid4().hex[:12]
                msgs = _messages(size, nonce)
                ttft, total, usage = await _one(client, url, model, msgs, auth)
                row[label] = (ttft, total, usage)
                prefill = usage.get("prompt_tokens", 0)
                print(f"{size:>7} {label:>7} {ttft:>8.2f} {total:>8.2f} {prefill:>11}")
            d, p = row["direct"], row["proxy"]
            print(f"{'':>7} {'hop':>7} {'':>8} {(p[1]-d[1])*1000:>8.0f} ms "
                  f"(ttft Δ {(p[0]-d[0])*1000:+.0f} ms)")


if __name__ == "__main__":
    asyncio.run(main())
