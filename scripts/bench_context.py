#!/usr/bin/env python3
"""Measure the context-cost optimizations on THIS tool inventory + config.

Reports, for a set of representative prompts:
  1. tool-schema tokens re-sent every turn under `all` vs `auto` exposure
     (using the real runtime.selector.ToolSelector + your runtime.yaml keywords);
  2. transcript-compaction savings on a simulated coding run.

No model or GPU needed. Rough token proxy = chars / 4 (good enough to compare).

    $JAYNET_HOME/.venv/bin/python scripts/bench_context.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from runtime.registry import ToolRegistry  # noqa: E402
from runtime.selector import ToolSelector  # noqa: E402
from runtime.loop import _compact_messages  # noqa: E402

TOK = 4  # chars per token (proxy)


def schema_tokens(reg, allow):
    schemas = reg.openai_schemas(allow)
    return len(json.dumps(schemas)) // TOK, len(schemas)


def main():
    cfg = yaml.safe_load(open(Path(__file__).resolve().parents[1] / "config/runtime.yaml"))
    reg = ToolRegistry("tools"); reg.discover()
    sel = ToolSelector(reg, cfg)
    total = len(reg.all())

    all_tok, _ = schema_tokens(reg, None)
    print(f"Tool inventory: {total} tools")
    print(f"Schema cost in `all` mode: ~{all_tok:,} tokens/turn (re-sent every turn)\n")

    prompts = [
        "implement a retry wrapper in src/net.py and run the tests",
        "what's the weather in Zurich and the latest news",
        "summarize this document for me",
        "commit my changes and push to origin",
        "why did the last run fail?",
    ]
    print(f"`auto` mode schema cost by prompt (mode in config: {cfg['tool_selection']['mode']}):")
    for p in prompts:
        allow = sel._auto(p, [t.name for t in reg.all()])  # deterministic heuristic
        tok, n = schema_tokens(reg, sorted(allow))
        save = 100 * (all_tok - tok) // all_tok if all_tok else 0
        print(f"  ~{tok:5,} tok ({n:2d} tools, -{save:2d}% vs all)  | {p[:48]}")

    # ---- compaction ----
    print("\nCompaction on a simulated 6-iteration coding run:")
    msgs = [{"role": "system", "content": "S" * 4000}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]})
        # a chunky tool result (e.g. fs.read of a file / a test log)
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "fs.read",
                     "content": json.dumps({"status": "ok", "result": {"content": "X" * 6000}})})
    before = sum(len(m.get("content") or "") for m in msgs) // TOK
    comp_cfg = {"enabled": True, "max_result_chars": 2000, "keep_last": 3}
    n = _compact_messages(msgs, comp_cfg)
    after = sum(len(m.get("content") or "") for m in msgs) // TOK
    print(f"  compacted {n} tool message(s); transcript ~{before:,} -> ~{after:,} tokens "
          f"(-{100*(before-after)//before}%)")
    print("\nNote: in `all` mode the schema prefix is largely a prompt-cache HIT on a")
    print("local server (cheap FLOPs), but it still occupies context + counts toward the")
    print("cumulative token budget. `auto` and compaction reduce both.")


if __name__ == "__main__":
    main()
