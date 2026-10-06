#!/usr/bin/env python3
"""check_doc_tools — every tool name the docs mention must exist (doc audit
2026-10-05, #14c).

Scans docs/*.md and README.md for backticked dotted names (`memory.search`)
and validates each against the tool registry (core tools/ + bundled plugin
tools, same discovery as gen_catalog). False-positive control — a candidate
is skipped when:

- it matches a real tool (pass), or
- its FIRST segment is a top-level config section of config/runtime.yaml
  (config keys like `agent.verify.stall_after`, `tools.test.sandbox_prefix`
  are the dominant lookalike class), or
- its last segment looks like a file extension (state.md, litellm.yaml,
  graph.json), or
- it is in the explicit SKIP set below (hand-reviewed residuals).

Usage:
  scripts/check_doc_tools.py    # fail (exit 1) on unknown tool names
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DOCS = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]

# Hand-reviewed residuals that are not tools and not caught by the
# config-root / extension heuristics. Keep SHORT — everything here is a
# documented blind spot.
SKIP = {
    # `model.switch` appears only in the sentence "There is no model.switch
    # tool" (code-map.md) — a negation, not a reference.
    "model.switch",
    # Python/runtime API references in contributor docs.
    "asyncio.create_task", "asyncio.to_thread", "sys.modules",
    "runtime.__version__", "runtime.jaypack.install_pack",
    "app.state.runtime.run", "runtime.run", "web.server",
    # Plugin-server instance attributes (plugins.md authoring section).
    "s.shutdown_hooks", "s.startup_hooks",
    # Eval-case grader namespace (expect.answer_exact_any, …) — eval YAML
    # fields, not tools.
    "expect.answer_exact_any",
    # Record fields: preset caps.* (model-placement.md), the measured
    # footprint record (llama-ops.md).
    "caps.thinking", "caps.vision", "measured.ram_gib",
    # Deliberate doc examples: the did-you-mean typo (testing-harness.md)
    # and a removed config key described as removed (testing.md).
    "agent.stall_check.aftr", "tools.code.container",
}

_EXT = {
    "md", "py", "yaml", "yml", "json", "jsonl", "txt", "conf", "db", "svg",
    "png", "gguf", "toml", "lock", "sh", "bash", "js", "css", "html", "mmd",
    "csv", "tsv", "pdf", "wav", "mp3", "zip", "tar", "gz", "exe", "service",
    "timer", "sock", "log", "env", "example", "jayplugin", "com", "io", "dev",
    "org", "net", "sh", "m4a", "pgm",
}

_CANDIDATE = re.compile(r"`([a-z][a-z0-9_]*(?:\.[a-z0-9_*-]+)+)`")


def valid_tools() -> set[str]:
    from runtime.registry import ToolRegistry
    names: set[str] = set()
    reg = ToolRegistry(ROOT / "tools")
    reg.discover()
    names |= set(reg._tools)
    for pdir in sorted((ROOT / "plugins").iterdir()):
        tdir = pdir / "tools"
        if not tdir.is_dir():
            continue
        preg = ToolRegistry(tdir)
        try:
            preg.discover_extra(tdir)
        except Exception:
            continue
        names |= set(preg._tools)
    return names


def config_paths() -> set[str]:
    """Every config key path in config/runtime.yaml AND every suffix of one
    — docs write nested keys in shorthand (`anchor.budget` for
    agent.anchor.budget, `deliverable_check.enabled`), so a candidate that
    IS any suffix of a real key path is a config reference, not a tool."""
    import yaml

    def leaves(d, pre=""):
        for k, v in (d or {}).items():
            p = f"{pre}.{k}" if pre else k
            yield p
            if isinstance(v, dict):
                yield from leaves(v, p)

    cfg = yaml.safe_load((ROOT / "config" / "runtime.yaml")
                         .read_text(encoding="utf-8"))
    out: set[str] = set()
    for path in leaves(cfg):
        parts = path.split(".")
        for i in range(len(parts)):
            out.add(".".join(parts[i:]))
    return out


def main() -> int:
    tools = valid_tools()
    cfg_paths = config_paths()
    plugin_names = {p.name for p in (ROOT / "plugins").iterdir() if p.is_dir()}
    bad: dict[str, set[str]] = {}
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for m in _CANDIDATE.finditer(text):
            name = m.group(1)
            first, _, last = name.rpartition(".")
            segs = name.split(".")
            if (name in tools or name in cfg_paths or last in _EXT
                    or name in SKIP or "*" in name):
                continue
            # plugins.<plugin>.<key> — a bundled plugin's own config keys.
            if len(segs) >= 3 and segs[0] == "plugins" \
                    and segs[1] in plugin_names:
                continue
            # A bare namespace prefix of real tools (e.g. `browser.browse`'s
            # family mention `browser.*` handled above; `memory.` prose).
            if any(t == name or t.startswith(name + ".") for t in tools):
                continue
            bad.setdefault(doc.name, set()).add(name)
    if bad:
        for doc_name, names in sorted(bad.items()):
            for n in sorted(names):
                print(f"{doc_name}: `{n}` — no such tool in the registry "
                      "(or add to SKIP with a reason)")
        print("check_doc_tools: FAIL — docs name tools that don't exist")
        return 1
    print(f"check_doc_tools: OK — {len(tools)} tools, "
          f"{len(DOCS)} docs scanned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
