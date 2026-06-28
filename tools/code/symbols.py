"""code.symbols — find where things are *defined* or *used*, returning handles.

`fs.grep` is plain text; this is code-aware navigation, the single biggest lever
for keeping context lean (read with line ranges, not whole files — see the
coding-projects skill). Two modes:

- definitions: where is symbol X defined? Backed by universal-ctags when present
  (real parsing across many languages); falls back to language-aware regex for
  Python/JS/TS/Go/Rust/C so it still works with no ctags installed.
- references: where is X used? A bounded, word-boundary grep that skips the usual
  noise dirs (.git, node_modules, venvs, caches).

Either way it returns compact `path:line` hits with the one matching line — never
file bodies — so the agent can then fs.read just the spans that matter. Confined
to the allowed roots and private (it reads local source).
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult, work_roots

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
              ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".tox"}

# Per-language "definition" patterns ({sym} substituted with the escaped symbol).
_DEF_PATTERNS = {
    ".py":  [r"^\s*def\s+{sym}\b", r"^\s*async\s+def\s+{sym}\b", r"^\s*class\s+{sym}\b",
             r"^\s*{sym}\s*="],
    ".js":  [r"\bfunction\s+{sym}\b", r"\b(?:const|let|var)\s+{sym}\b", r"\bclass\s+{sym}\b"],
    ".jsx": [r"\bfunction\s+{sym}\b", r"\b(?:const|let|var)\s+{sym}\b", r"\bclass\s+{sym}\b"],
    ".ts":  [r"\bfunction\s+{sym}\b", r"\b(?:const|let|var)\s+{sym}\b", r"\bclass\s+{sym}\b",
             r"\binterface\s+{sym}\b", r"\btype\s+{sym}\b", r"\benum\s+{sym}\b"],
    ".tsx": [r"\bfunction\s+{sym}\b", r"\b(?:const|let|var)\s+{sym}\b", r"\bclass\s+{sym}\b",
             r"\binterface\s+{sym}\b", r"\btype\s+{sym}\b"],
    ".go":  [r"\bfunc\s+(?:\([^)]*\)\s*)?{sym}\b", r"\btype\s+{sym}\b"],
    ".rs":  [r"\bfn\s+{sym}\b", r"\bstruct\s+{sym}\b", r"\benum\s+{sym}\b",
             r"\btrait\s+{sym}\b", r"\bconst\s+{sym}\b"],
    ".c":   [r"\b{sym}\s*\(", r"\bstruct\s+{sym}\b"],
    ".h":   [r"\b{sym}\s*\(", r"\bstruct\s+{sym}\b", r"#define\s+{sym}\b"],
    ".cpp": [r"\b{sym}\s*\(", r"\b(?:class|struct)\s+{sym}\b"],
}


def _allowed_roots(ctx: ToolContext) -> list[Path]:
    return work_roots(ctx)


def _resolve_scope(ctx: ToolContext, path: str | None) -> Path:
    roots = _allowed_roots(ctx)
    p = Path(path or roots[0]).expanduser().resolve()
    if not any(p == r or r in p.parents for r in roots):
        allowed = ", ".join(str(r) for r in roots)
        raise PermissionError(f"path {p} is outside the allowed roots ({allowed}).")
    if not p.exists():
        raise FileNotFoundError(f"path does not exist: {p}")
    return p


def _iter_files(root: Path, exts: set[str] | None):
    if root.is_file():
        yield root
        return
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if exts and p.suffix not in exts:
            continue
        yield p


async def _ctags_defs(root: Path, symbol: str) -> list[dict] | None:
    """Use universal-ctags JSON output if available; else None to signal fallback."""
    ctags = shutil.which("ctags")
    if not ctags:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            ctags, "-R", "--output-format=json", "-f", "-",
            "--fields=+n", str(root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (asyncio.TimeoutError, Exception):
        return None
    import json
    hits = []
    for line in out.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            tag = json.loads(line)
        except ValueError:
            continue
        if tag.get("name") != symbol:
            continue
        hits.append({
            "path": tag.get("path"),
            "line": tag.get("line"),
            "kind": tag.get("kind"),
            "text": (tag.get("pattern") or "").strip("/^$ "),
        })
    return hits


class CodeSymbols(Tool):
    name = "code.symbols"
    description = (
        "Code-aware navigation: find where a symbol is DEFINED (mode=definitions) "
        "or USED (mode=references) and get back compact path:line hits with the "
        "matching line — not file bodies. Use this to locate code before reading, "
        "so you fs.read only the spans you need. Backed by ctags when available, "
        "with a language-aware regex fallback."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Identifier to look for."},
            "mode": {"type": "string", "enum": ["definitions", "references"],
                     "default": "definitions"},
            "path": {"type": "string",
                     "description": "File or directory to search. Defaults to first allowed root."},
            "exts": {"type": "array", "items": {"type": "string"},
                     "description": "Limit to these file extensions, e.g. ['.py', '.ts']."},
            "max_hits": {"type": "integer", "default": 50, "minimum": 1, "maximum": 300},
        },
        "required": ["symbol"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        symbol = args["symbol"].strip()
        if not symbol or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", symbol):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="symbol must be a bare identifier")
        try:
            scope = _resolve_scope(ctx, args.get("path"))
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))

        mode = args.get("mode", "definitions")
        exts = set(args.get("exts") or []) or None
        max_hits = int(args.get("max_hits", 50))
        backend = "regex"
        hits: list[dict] = []

        if mode == "definitions":
            tagged = await _ctags_defs(scope, symbol)
            if tagged is not None:
                backend = "ctags"
                # ctags scans everything; apply ext filter + cap here.
                for h in tagged:
                    if exts and Path(h["path"] or "").suffix not in exts:
                        continue
                    hits.append(h)
                    if len(hits) >= max_hits:
                        break
            if backend == "regex":
                hits = self._regex_defs(scope, symbol, exts, max_hits)
        else:  # references
            hits = self._regex_refs(scope, symbol, exts, max_hits)

        return ToolResult(status="ok", result={
            "symbol": symbol, "mode": mode, "backend": backend,
            "scope": str(scope), "count": len(hits),
            "hits": hits[:max_hits],
            "truncated": len(hits) >= max_hits,
        }, tool_name=self.name)

    def _regex_defs(self, scope: Path, symbol: str, exts, cap: int) -> list[dict]:
        esc = re.escape(symbol)
        hits: list[dict] = []
        for f in _iter_files(scope, exts):
            pats = _DEF_PATTERNS.get(f.suffix)
            if not pats:
                continue
            compiled = [re.compile(p.format(sym=esc)) for p in pats]
            try:
                for i, line in enumerate(f.read_text("utf-8", "replace").splitlines(), 1):
                    if any(c.search(line) for c in compiled):
                        hits.append({"path": str(f), "line": i, "text": line.strip()[:200]})
                        if len(hits) >= cap:
                            return hits
            except (OSError, UnicodeError):
                continue
        return hits

    def _regex_refs(self, scope: Path, symbol: str, exts, cap: int) -> list[dict]:
        word = re.compile(rf"\b{re.escape(symbol)}\b")
        hits: list[dict] = []
        for f in _iter_files(scope, exts):
            try:
                for i, line in enumerate(f.read_text("utf-8", "replace").splitlines(), 1):
                    if word.search(line):
                        hits.append({"path": str(f), "line": i, "text": line.strip()[:200]})
                        if len(hits) >= cap:
                            return hits
            except (OSError, UnicodeError):
                continue
        return hits
