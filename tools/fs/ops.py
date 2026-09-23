"""Filesystem tools — let the agent read, search and edit code/data on the box.

Code-aware: fs.read returns line numbers (rendered to the model as plain text —
small models copy JSON escaping verbatim into later edits), fs.grep returns
file:line hits, fs.edit is a unique-match string replace (the safe way to patch
a file) with forgiving matching for read-output artifacts. All operations
are confined to tools.fs.allowed_roots — a path outside them is refused.

Marked private: file contents are local/proprietary and will not be forwarded to
remote LLM tools unless the run sets share_private. Mutating ops (write, edit)
declare requires_confirmation so the loop's confirmation gate pauses on them.
"""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path

from runtime import hooks as _hooks
from runtime.tool_base import (
    Tool,
    ToolContext,
    ToolResult,
)
from runtime.tool_base import (
    resolve_in_roots as tb_resolve_in_roots,
)
from runtime.tool_base import (
    work_roots as tb_work_roots,
)

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache"}

_DIFF_MAX_LINES = 40   # keep the result (and the chat row) small


def _short_diff(old: str, new: str, path: str) -> dict:
    """Compact unified diff of an edit: hunk headers + +/- lines, capped. The
    counts and diff ride in the tool result — the model gets confirmation and
    the chat UI renders the short diff inline."""
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), n=2, lineterm=""))
    body = [l for l in lines if not l.startswith(("---", "+++"))]
    added = sum(1 for l in body if l.startswith("+"))
    removed = sum(1 for l in body if l.startswith("-"))
    truncated = len(body) > _DIFF_MAX_LINES
    if truncated:
        body = body[:_DIFF_MAX_LINES] + [f"… (diff truncated, {len(lines)} lines total)"]
    return {"action": "edited", "path": path, "added": added, "removed": removed,
            "diff": "\n".join(body), "diff_truncated": truncated}


def _roots(ctx: ToolContext) -> list[Path]:
    # Single source of truth (runtime.tool_base.work_roots): the run's work_root
    # (project files dir / per-chat scratch) + ephemeral tmp_root. archives.* and
    # code.* resolve through the same helper, so the boundary is uniform.
    return tb_work_roots(ctx)


def _resolve(ctx: ToolContext, path: str, must_exist: bool = True) -> Path:
    return tb_resolve_in_roots(_roots(ctx), path, must_exist)


def _fire_project_changed(ctx: ToolContext, p: Path) -> None:
    """Dirty project-scoped plugin state (graphify's graph) when the agent's own
    fs.* write lands inside a project-bound run — the web API already fires this
    hook; without it the graph goes stale after agent-heavy runs. work_root IS
    the project's resolved files dir (web/routes_run.py), so its parents give
    the true projects root even with web.projects_dir overrides. No-op off the
    project path (CLI, scratch chats) and for writes outside the project tree
    (tmp_root scratch)."""
    if not ctx.project_id or not ctx.work_root:
        return
    wr = Path(ctx.work_root).resolve()
    try:
        if not p.resolve().is_relative_to(wr):
            return
    except OSError:
        return
    _hooks.fire("on_project_file_changed", ctx.owner, ctx.project_id,
                str(p), str(wr.parents[2]))


class _FsReadResult(ToolResult):
    """fs.read payload rendered to the model as PLAIN TEXT (one header line +
    the raw line-numbered content) instead of a JSON dump. Trace evidence:
    small models copied the JSON escaping (\\t, \\n, quotes) AND the
    line-number prefixes verbatim into fs.edit old_str. The `result` dict
    keeps its exact shape (path/lines/truncated_bytes/content) for
    programmatic consumers — trace rows, the chat UI, tests; only this
    serialization changes."""
    def to_model_message(self) -> str:
        if self.status == "error" or not isinstance(self.result, dict):
            return super().to_model_message()
        r = self.result
        header = f"# {Path(str(r.get('path', ''))).name} (lines {r.get('lines', '?')})"
        if r.get("truncated_bytes"):
            header += " [truncated by max_bytes]"
        s = f"{header}\n{r.get('content', '')}"
        if len(s) > 20000:   # same soft cap as ToolResult.to_model_message
            s = s[:20000] + "\n… (truncated to 20000 chars)"
        return s


class FsRead(Tool):
    name = "fs.read"
    description = ("Read a text file. Returns plain text with line numbers. Use "
                  "start_line/end_line to read a slice of a large file. Bounded "
                  "by max_bytes.")
    private = True
    read_only = True
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
        size = p.stat().st_size
        raw = p.read_bytes()[:max_bytes]
        text = raw.decode("utf-8", "replace")
        lines = text.splitlines()
        start = int(args.get("start_line", 1))
        end = int(args.get("end_line", len(lines)))
        start = max(1, start)
        end = min(len(lines), end)
        numbered = "\n".join(f"{i:>6}\t{lines[i - 1]}" for i in range(start, end + 1))
        return _FsReadResult(status="ok", result={
            "path": str(p),
            "lines": f"{start}-{end} of {len(lines)}",
            "truncated_bytes": size > max_bytes,
            "content": numbered,
        })


class FsList(Tool):
    name = "fs.list"
    description = ("List a directory tree up to `depth` levels. Optionally filter "
                  "by glob. Skips .git/__pycache__/node_modules/.venv.")
    private = True
    read_only = True
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
    read_only = True
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
                  "file:line: matches. Use glob to narrow file types. For "
                  "counting or aggregating across many files (totals, "
                  "top-N, per-file stats) use ONE code.run script instead — "
                  "repeated per-file fs.grep calls burn your iteration "
                  "budget.")
    private = True
    read_only = True
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
                  "directories. Use fs.edit for surgical changes to an existing "
                  "file. Keep each call's content SMALL: for large files, write "
                  "the first chunk with mode=overwrite, then add the rest with "
                  "several mode=append calls — one giant argument will be "
                  "rejected by the server.")
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
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = args.get("mode", "overwrite")
        with p.open("a" if mode == "append" else "w", encoding="utf-8") as f:
            f.write(args["content"])
        action = "appended" if mode == "append" else ("overwritten" if existed else "created")
        _fire_project_changed(ctx, p)
        return ToolResult(status="ok", result={"path": str(p), "mode": mode,
                                                "action": action,
                                                "lines": len(args["content"].splitlines()),
                                                "bytes": len(args["content"].encode())})


_LINE_PREFIX_RX = re.compile(r"^\s*\d+(?:\t|:\s?)")


def _strip_line_prefixes(s: str) -> str:
    """Remove fs.read-style line-number prefixes (`     3\\t` / `3: `) that
    small models copy from read output into their edit strings. Only ever
    consulted AFTER an exact match has failed, so legitimate file content
    that starts with digits+tab is never mangled."""
    return "\n".join(_LINE_PREFIX_RX.sub("", l) for l in s.split("\n"))


def _find_exact(text: str, needle: str) -> list[int]:
    """Start offsets of every exact occurrence of needle in text."""
    if not needle:
        return []
    out, i = [], text.find(needle)
    while i != -1:
        out.append(i)
        i = text.find(needle, i + 1)
    return out


def _ws_fuzzy_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """Whitespace-normalized search: every whitespace run (on BOTH sides) counts
    as one space. Returns (start, end) spans into the REAL text, so the edit
    splices the original bytes — never a normalized copy."""
    tokens = [re.escape(t) for t in re.split(r"\s+", needle.strip()) if t]
    if not tokens:
        return []
    rx = re.compile(r"\s+".join(tokens))
    return [(m.start(), m.end()) for m in rx.finditer(text)]


def _ambiguous_error(text: str, starts: list[int], flavor: str = "") -> str:
    lines = ", ".join(str(text.count("\n", 0, s) + 1) for s in starts[:10])
    if len(starts) > 10:
        lines += ", …"
    return (f"old_str matches {len(starts)} times{flavor} (lines {lines}); "
            "add more context to make it unique")


def _closest_snippet(text: str, old: str) -> str | None:
    """Best-matching region of the file for a genuinely-missed old_str, shown
    with fs.read's line-number format so the model can self-correct in ONE
    retry instead of blind guessing."""
    file_lines = text.splitlines()
    old_lines = old.strip().splitlines()
    if not file_lines or not old_lines:
        return None
    k = min(len(old_lines), len(file_lines))
    probe = "\n".join(old_lines[:k])
    sm = difflib.SequenceMatcher()
    sm.set_seq2(probe)
    best_i, best_r = 0, 0.0
    for i in range(len(file_lines) - k + 1):
        sm.set_seq1("\n".join(file_lines[i:i + k]))
        if sm.real_quick_ratio() <= best_r or sm.quick_ratio() <= best_r:
            continue
        r = sm.ratio()
        if r > best_r:
            best_r, best_i = r, i
    if best_r <= 0:
        return None
    show = min(max(k, 1), 5)
    body = "\n".join(f"{best_i + j + 1:>6}\t{file_lines[best_i + j][:200]}"
                     for j in range(show))
    more = f"\n      … ({k - show} more lines)" if k > show else ""
    return f"closest match (lines {best_i + 1}-{best_i + k}):\n{body}{more}"


class FsEdit(Tool):
    name = "fs.edit"
    description = ("Replace a unique string in a file with a new one. old_str must "
                  "match exactly once (include enough surrounding context to be "
                  "unique). Matching is forgiving: copied fs.read line-number "
                  "prefixes and whitespace drift are tolerated automatically, "
                  "and a miss returns the closest matching region so you can "
                  "correct and retry once. Fails if it matches multiple times.")
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
        old, new = args["old_str"], args["new_str"]
        note = None
        span: tuple[int, int] | None = None
        # Fallback order: exact → line-prefix-stripped → whitespace-normalized
        # → closest-snippet error. Exact is always tried first, so a legitimate
        # old_str that itself starts with digits+tab is never "corrected".
        starts = _find_exact(text, old)
        if len(starts) > 1:
            return ToolResult(status="error", result=None,
                              error=_ambiguous_error(text, starts))
        if len(starts) == 1:
            span = (starts[0], starts[0] + len(old))
        else:
            stripped = _strip_line_prefixes(old)
            base = stripped if stripped != old else old
            if stripped != old:
                s2 = _find_exact(text, stripped)
                if len(s2) > 1:
                    return ToolResult(status="error", result=None,
                                      error=_ambiguous_error(
                                          text, s2,
                                          " after stripping line-number prefixes"))
                if len(s2) == 1:
                    span = (s2[0], s2[0] + len(stripped))
                    note = ("matched after stripping fs.read line-number "
                            "prefixes from old_str")
                    ns = _strip_line_prefixes(new)
                    if ns != new:
                        new = ns
                        note += "; new_str prefixes stripped too"
            if span is None:
                spans = _ws_fuzzy_spans(text, base)
                if len(spans) > 1:
                    return ToolResult(status="error", result=None,
                                      error=_ambiguous_error(
                                          text, [s for s, _ in spans],
                                          " after whitespace normalization"))
                if len(spans) == 1:
                    span = spans[0]
                    note = "matched after whitespace normalization"
            if span is None:
                err = "old_str not found"
                snip = _closest_snippet(text, base)
                if snip:
                    err += f"; {snip}"
                return ToolResult(status="error", result=None, error=err)
        new_text = text[:span[0]] + new + text[span[1]:]
        p.write_text(new_text, encoding="utf-8")
        _fire_project_changed(ctx, p)
        info = _short_diff(text, new_text, str(p))
        info["replaced"] = 1
        if note:
            info["note"] = note
        return ToolResult(status="ok", result=info)
