"""Budget tracking for an agent run.

A `Budget` is created per request and consumed as the loop runs. When any
ceiling is hit, `check()` raises `BudgetExceeded` and the loop terminates
gracefully.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    """Raised when any budget ceiling is hit."""

    def __init__(self, reason: str, details: dict):
        super().__init__(reason)
        self.reason = reason
        self.details = details


@dataclass
class Budget:
    max_iterations: int
    max_wall_clock_s: float
    max_cost_usd: float
    max_total_tokens: int
    # Weight applied to cached prompt tokens in total_tokens (0.1 ≈ cloud
    # cached-input pricing; 1.0 = count prefix-cache hits at full weight).
    cached_token_weight: float = 0.1

    # Consumption counters
    iterations: int = 0
    cost_usd: float = 0.0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_cached: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def total_tokens(self) -> int:
        """Effective tokens charged against max_total_tokens.

        Prompt tokens served from the server-side prefix cache (a KV lookup,
        not a re-prefill) are near-free on local llama.cpp and billed at a
        fraction by cloud providers. In long tool loops >90% of prompt tokens
        are cache hits, so full-weight counting burned the ceiling on
        re-transmission rather than real work. Raw counters stay untouched
        for accounting; only the ceiling/pressure view is weighted.
        """
        uncached = max(0, self.tokens_prompt - self.tokens_cached)
        return int(uncached + self.tokens_cached * self.cached_token_weight
                   + self.tokens_completion)

    def pressure(self) -> tuple[float, str]:
        """Highest fraction of any ceiling consumed so far, with the dominant
        dimension's name. Used to warn the agent before it gets cut off."""
        dims = {
            "iteration": (self.iterations / self.max_iterations) if self.max_iterations else 0.0,
            "time": (self.elapsed_s / self.max_wall_clock_s) if self.max_wall_clock_s else 0.0,
            "cost": (self.cost_usd / self.max_cost_usd) if self.max_cost_usd else 0.0,
            "token": (self.total_tokens / self.max_total_tokens) if self.max_total_tokens else 0.0,
        }
        name = max(dims, key=dims.get)
        return dims[name], name

    def tick(self) -> None:
        """Call at the start of each loop iteration."""
        self.iterations += 1
        self.check()

    def add_usage(self, model: str, prompt: int, completion: int,
                  cached: int = 0, cost_table: dict | None = None) -> None:
        """Record token usage and update cost estimate."""
        self.tokens_prompt += prompt
        self.tokens_completion += completion
        self.tokens_cached += cached
        if cost_table and model in cost_table:
            rates = cost_table[model]
            # Cached input is typically 10% of full input cost (Anthropic).
            billable_prompt = max(0, prompt - cached) + cached * 0.1
            self.cost_usd += (billable_prompt * rates["input"] / 1_000_000)
            self.cost_usd += (completion * rates["output"] / 1_000_000)

    def check(self) -> None:
        """Raise BudgetExceeded if any ceiling is hit. 0 = no ceiling for
        every limit (the admin budget editor's "off" state); wall clock
        documented it first (local-first: stall/iterations/tokens guard
        local runs instead), matching pressure()'s guards."""
        if self.max_iterations and self.iterations > self.max_iterations:
            raise BudgetExceeded("max_iterations", {
                "iterations": self.iterations, "limit": self.max_iterations,
            })
        if self.max_wall_clock_s and self.elapsed_s > self.max_wall_clock_s:
            raise BudgetExceeded("max_wall_clock_s", {
                "elapsed_s": round(self.elapsed_s, 1), "limit": self.max_wall_clock_s,
            })
        if self.max_cost_usd and self.cost_usd > self.max_cost_usd:
            raise BudgetExceeded("max_cost_usd", {
                "cost_usd": round(self.cost_usd, 4), "limit": self.max_cost_usd,
            })
        if self.max_total_tokens and self.total_tokens > self.max_total_tokens:
            raise BudgetExceeded("max_total_tokens", {
                "total_tokens": self.total_tokens, "limit": self.max_total_tokens,
            })

    def summary(self) -> dict:
        return {
            "iterations": self.iterations,
            "elapsed_s": round(self.elapsed_s, 2),
            "cost_usd": round(self.cost_usd, 4),
            "tokens": {
                "prompt": self.tokens_prompt,
                "completion": self.tokens_completion,
                "cached": self.tokens_cached,
                "total": self.total_tokens,
            },
        }
