"""Filesystem tools — let the agent read, search and edit code/data on the box.

Code-aware: fs.read returns line numbers, fs.grep returns file:line hits, fs.edit
is a unique-match string replace (the safe way to patch a file). All operations
are confined to tools.fs.allowed_roots — a path outside them is refused.

Marked private: file contents are local/proprietary and will not be forwarded to
remote LLM tools unless the run sets share_private. Mutating ops (write, edit)
declare requires_confirmation so the loop's confirmation gate pauses on them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from runtime.tool_base import (
    Tool, ToolContext, ToolResult,
    work_roots as tb_work_roots, resolve_in_roots as tb_resolve_in_roots,
)

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache"}


def _cfg(ctx: ToolContext) -> dict:
    return ctx.config.get("tools", {}).get("fs", {})


def _roots(ctx: ToolContext) -> list[Path]:
    # Single source of truth (runtime.tool_base.work_roots): the run's work_root
    # (project files dir / per-chat scratch) + ephemeral tmp_root. archives.* and
    # code.* resolve through the same helper, so the boundary is uniform.
    return tb_work_roots(ctx)


def _resolve(ctx: ToolContext, path: str, must_exist: bool = True) -> Path:
    return tb_resolve_in_roots(_roots(ctx), path, must_exist)


class FsRead(Tool):
    name = "fs.read"
    description = ("Read a text file. Returns content with line numbers. Use "
                  "start_line/end_line to read a slice of a large file. Bounded "
                  "by max_bytes.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1,
                           "description": "1-based first line to return."},
            "end_line": {"type": "integer", "minimum": 1,
                         "description": "1-based last line (inclusive)."},
            "max_bytes": {"type": "integer", "default": 100000, "minimum": 1,
                          "maximum": 1000000},
        },
        "required": ["path"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            p = _resolve(ctx, args["path"])
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None, error=str(e))
        max_bytes = min(int(args.get("max_bytes", 100000)), 1_000_000)
        raw = p.read_bytes()[:max_bytes]
        text = raw.decode("utf-8", "replace")
        lines = text.splitlines()
        start = int(args.get("start_line", 1))
        end = int(args.get("end_line", len(lines)))
        start = max(1, start)
        end = min(len(lines), end)
        numbered = "\n".join(f"{i:>6}\t{lines[i - 1]}" for i in range(start, end + 1))
        return ToolResult(status="ok", result={
            "path": str(p),
            "lines": f"{start}-{end} of {len(lines)}",
            "truncated_bytes": len(p.read_bytes()) > max_bytes,
            "content": numbered,
        })


class FsList(Tool):
    name = "fs.list"
    description = ("List a directory tree up to `depth` levels. Optionally filter "
                  "by glob. Skips .git/__pycache__/node_modules/.venv.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "depth": {"type": "integer", "default": 2, "minimum": 1, "maximum": 6},
            "glob": {"type": "string", "description": "e.g. '*.py' to filter files."},
            "include_hidden": {"type": "boolean", "default": False},
        },
        "required": ["path"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            base = _resolve(ctx, args["path"])
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None, error=str(e))
        if not base.is_dir():
            return ToolResult(status="error", result=None, error=f"not a directory: {base}")
        depth = int(args.get("depth", 2))
        pattern = args.get("glob")
        include_hidden = bool(args.get("include_hidden"))
        entries = []
        base_depth = len(base.parts)
        for root, dirs, files in os.walk(base):
            rootp = Path(root)
            if len(rootp.parts) - base_depth >= depth:
                dirs[:] = []
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS
                       and (include_hidden or not d.startswith("."))]
            for name in sorted(dirs):
                entries.append(str((rootp / name).relative_to(base)) + "/")
            for name in sorted(files):
                if not include_hidden and name.startswith("."):
                    continue
                if pattern and not Path(name).match(pattern):
                    continue
                entries.append(str((rootp / name).relative_to(base)))
            if len(entries) > 2000:
                entries.append("… (truncated at 2000 entries)")
                break
        return ToolResult(status="ok", result={"base": str(base), "count": len(entries),
                                                "entries": entries})


class FsFind(Tool):
    name = "fs.find"
    description = (
        "Find files by NAME anywhere under a directory (recursive). Use this to "
        "LOCATE a file before you read / convert / deliver it, instead of guessing "
        "its path. `query` is a filename glob ('*.md', 'Student_Overview*') or a "
        "plain substring ('overview', case-insensitive); it returns the matching "
        "relative paths. For searching file CONTENTS, use fs.grep instead."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "filename glob or substring to match"},
            "path": {"type": "string", "description": "directory to search under (default '.')"},
        },
        "required": ["query"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            base = _resolve(ctx, args.get("path") or ".")
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None, error=str(e))
        if not base.is_dir():
            return ToolResult(status="error", result=None, error=f"not a directory: {base}")
        q = (args.get("query") or "").strip()
        if not q:
            return ToolResult(status="error", result=None, error="query is required")
        is_glob = any(c in q for c in "*?[")
        ql = q.lower()
        hits = []
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for name in sorted(files):
                if name.startswith("."):
                    continue
                ok = Path(name).match(q) if is_glob else (ql in name.lower())
                if ok:
                    hits.append(str((Path(root) / name).relative_to(base)))
            if len(hits) >= 500:
                hits.append("… (truncated at 500)")
                break
        return ToolResult(status="ok", result={"query": q, "count": len(hits), "matches": hits})


class FsGrep(Tool):
    name = "fs.grep"
    description = ("Search files under a path for a regex pattern. Returns "
                  "file:line: matches. Use glob to narrow file types.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regex."},
            "path": {"type": "string"},
            "glob": {"type": "string", "default": "*", "description": "File glob, e.g. '*.py'."},
            "ignore_case": {"type": "boolean", "default": False},
            "max_matches": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
        },
        "required": ["pattern", "path"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            base = _resolve(ctx, args["path"])
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None, error=str(e))
        try:
            flags = re.IGNORECASE if args.get("ignore_case") else 0
            rx = re.compile(args["pattern"], flags)
        except re.error as e:
            return ToolResult(status="error", result=None, error=f"bad regex: {e}")
        pattern = args.get("glob", "*")
        max_matches = int(args.get("max_matches", 100))
        targets = [base] if base.is_file() else None
        if targets is None:
            targets = []
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
                for name in files:
                    if Path(name).match(pattern):
                        targets.append(Path(root) / name)
        matches, truncated = [], False
        for fp in targets:
            try:
                with fp.open("r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            matches.append(f"{fp}:{i}: {line.rstrip()[:300]}")
                            if len(matches) >= max_matches:
                                truncated = True
                                break
            except (OSError, UnicodeError):
                continue
            if truncated:
                break
        return ToolResult(status="ok", result={"pattern": args["pattern"],
                                                "match_count": len(matches),
                                                "truncated": truncated, "matches": matches})


class FsWrite(Tool):
    name = "fs.write"
    description = ("Write content to a file (overwrite or append). Creates parent "
                  "directories. Use fs.edit for surgical changes to an existing file.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["overwrite", "append"],
                     "default": "overwrite"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            p = _resolve(ctx, args["path"], must_exist=False)
        except PermissionError as e:
            return ToolResult(status="error", result=None, error=str(e))
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = args.get("mode", "overwrite")
        with p.open("a" if mode == "append" else "w", encoding="utf-8") as f:
            f.write(args["content"])
        return ToolResult(status="ok", result={"path": str(p), "mode": mode,
                                                "bytes": len(args["content"].encode())})


class FsEdit(Tool):
    name = "fs.edit"
    description = ("Replace a unique string in a file with a new one. old_str must "
                  "match exactly once (include enough surrounding context to be "
                  "unique). Fails if it matches zero or multiple times.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_str": {"type": "string", "description": "Exact text to replace (unique)."},
            "new_str": {"type": "string", "description": "Replacement text."},
        },
        "required": ["path", "old_str", "new_str"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            p = _resolve(ctx, args["path"])
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None, error=str(e))
        text = p.read_text(encoding="utf-8", errors="replace")
        n = text.count(args["old_str"])
        if n == 0:
            return ToolResult(status="error", result=None, error="old_str not found")
        if n > 1:
            return ToolResult(status="error", result=None,
                              error=f"old_str matches {n} times; add more context to make it unique")
        p.write_text(text.replace(args["old_str"], args["new_str"]), encoding="utf-8")
        return ToolResult(status="ok", result={"path": str(p), "replaced": 1})
