"""Coding context pack — repo map + project instructions for coding sub-agents.

A coding child (code.delegate, the architect's planner/executor) starts with an
EMPTY context: it doesn't know the repo's layout, its conventions, or its test
commands, and a local coder model burns its first iterations re-discovering them
with fs.list/fs.read chains. This module builds the two orientation artifacts
the serious coding agents (Aider's repo map, CLAUDE.md/AGENTS.md) rely on:

- **Repo map** — one line per source file: path, its imports, its top-level
  symbols. Alphabetical and content-stable, so it stays prompt-cache friendly;
  cached and only rebuilt when the tree's fingerprint (count/size/mtime)
  changes. Char-budgeted; on overflow the tail is truncated with a note.
- **Project instructions** — the first of JAYNET.md / AGENTS.md / CLAUDE.md at
  the work root, verbatim (capped). Conventions and commands the repo owner
  already wrote down for agents.

`coding_context()` combines both into the block prepended to coding spawn
prompts. Regex-based extraction (no ctags dependency) — orientation, not
precision; the child navigates with code.symbols for the real thing.
"""

from __future__ import annotations

import re
from pathlib import Path

_CODE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs",
              ".c", ".h", ".cpp", ".hpp"}
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
              ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
              ".tox", ".jaynet-worktrees"}
_INSTRUCTION_FILES = ("JAYNET.md", "AGENTS.md", "CLAUDE.md")
_MAX_FILES = 300
_MAX_FILE_BYTES = 300_000
_INSTRUCTION_CAP = 4000

_DEF_RES = [
    re.compile(p) for p in (
        r"^\s*(?:async\s+)?def\s+(\w+)",                    # python
        r"^\s*class\s+(\w+)",                              # python / js-ish
        r"^\s*(?:export\s+)?(?:default\s+)?function\s+(\w+)",  # js/ts
        r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=",    # js/ts
        r"^\s*(?:export\s+)?(?:interface|type|enum)\s+(\w+)",  # ts
        r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)",              # go
        r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait)\s+(\w+)",    # rust
        r"^\s*(?:[\w*]+\s+)+(\w+)\s*\([^)]*\)\s*\{?",      # c/cpp (loose)
    )
]
_IMPORT_RES = [
    re.compile(p) for p in (
        r"^\s*(?:from\s+\S+\s+)?import\s+(.+)",            # python
        r"^\s*import\s+.*?from\s+['\"]([^'\"]+)['\"]",     # js/ts
        r"^\s*import\s+['\"]([^'\"]+)['\"]",               # js/ts bare
        r"^\s*import\s+[\(\w]",                            # go (block marker)
        r"^\s*use\s+([\w:]+)",                             # rust
        r"^\s*#include\s+[<\"]([^>\"]+)[>\"]",             # c/cpp
    )
]


def _iter_code_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _CODE_EXTS:
            continue
        rel = p.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        yield p, rel


def _file_line(text: str) -> str:
    """One orientation line: top-level symbol names + condensed imports."""
    symbols, imports = [], []
    for line in text.splitlines()[:400]:           # headers carry the shape
        for rx in _DEF_RES:
            m = rx.match(line)
            if m:
                name = m.group(1)
                if not name.startswith("_") and name not in symbols:
                    symbols.append(name)
                break
        for rx in _IMPORT_RES:
            m = rx.match(line)
            if m:
                mod = (m.group(1) if m.lastindex else "deps").split(",")[0]
                mod = mod.strip()[:30]
                if mod and mod not in imports:
                    imports.append(mod)
                break
        if len(symbols) >= 8 and len(imports) >= 4:
            break
    out = ", ".join(symbols[:8]) or "—"
    if imports:
        out += "  ⟵ " + ", ".join(imports[:4])
    return out


def _fingerprint(root: Path) -> tuple:
    count, total, newest = 0, 0, 0
    for p, _ in _iter_code_files(root):
        try:
            st = p.stat()
        except OSError:
            continue
        count += 1
        total += st.st_size
        newest = max(newest, st.st_mtime_ns)
    return count, total, newest


_cache: dict[tuple, str] = {}


def repo_map(root: str | Path, max_chars: int = 6000) -> str:
    """One line per source file under root, char-budgeted. Cached on the
    tree's fingerprint so repeated spawns in one session don't re-scan."""
    root = Path(root)
    if not root.is_dir():
        return ""
    key = (str(root), max_chars, _fingerprint(root))
    if key in _cache:
        return _cache[key]
    lines, used, omitted = [], 0, 0
    for p, rel in _iter_code_files(root):
        if len(lines) >= _MAX_FILES:       # cap: count the rest, don't read
            omitted += 1
            continue
        try:
            if p.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        line = f"{rel}: {_file_line(text)}"
        if used + len(line) + 1 > max_chars:
            omitted += 1
            continue
        lines.append(line)
        used += len(line) + 1
    if omitted:
        lines.append(f"… ({omitted} more file{'s' if omitted != 1 else ''} "
                     "omitted — navigate with code.symbols/fs.find)")
    out = "\n".join(lines)
    if len(_cache) > 8:
        _cache.clear()
    _cache[key] = out
    return out


def project_instructions(root: str | Path) -> str:
    """The first agent-instructions file at the work root, verbatim (capped)."""
    root = Path(root)
    for name in _INSTRUCTION_FILES:
        p = root / name
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    return f"{name}:\n{text[:_INSTRUCTION_CAP]}"
        except OSError:
            continue
    return ""


def coding_context(work_root, config: dict) -> str:
    """The combined orientation block for a coding spawn prompt ('' if empty).

    Budget via tools.code.repomap.max_chars (default 6000 ≈ 1.5k tokens);
    enabled: false disables the repo map (project instructions still ride).
    """
    if not work_root:
        return ""
    cfg = ((config or {}).get("tools", {}).get("code", {}) or {}).get("repomap", {}) or {}
    try:
        max_chars = int(cfg.get("max_chars", 6000))
    except (TypeError, ValueError):
        max_chars = 6000
    parts = []
    if cfg.get("enabled", True):
        rm = repo_map(work_root, max_chars)
        if rm:
            parts.append("REPO MAP (orientation — navigate precisely with "
                         "code.symbols/fs.read):\n" + rm)
    instr = project_instructions(work_root)
    if instr:
        parts.append("PROJECT INSTRUCTIONS (the repo owner's rules — follow them):\n"
                     + instr)
    return "\n\n".join(parts)
