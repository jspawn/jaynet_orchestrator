"""code.tree — a cheap structural map of a directory, to orient a fresh run.

The coding-projects skill resets context between units; at the start of a unit the
agent often needs a quick "what's in here" without paying to fs.list every folder.
This returns a compact, depth-bounded tree (dirs + files, with sizes), skipping
the usual noise dirs, so one cheap call replaces a flurry of fs.list calls. Read,
private, never returns file contents.
"""

from __future__ import annotations

from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult, work_roots

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
              ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".tox"}


def _allowed_roots(ctx: ToolContext) -> list[Path]:
    return work_roots(ctx)


def _resolve(ctx: ToolContext, path: str | None) -> Path:
    roots = _allowed_roots(ctx)
    p = Path(path or roots[0]).expanduser().resolve()
    if not any(p == r or r in p.parents for r in roots):
        allowed = ", ".join(str(r) for r in roots)
        raise PermissionError(f"path {p} is outside the allowed roots ({allowed}).")
    if not p.exists():
        raise FileNotFoundError(f"path does not exist: {p}")
    return p


def _human(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n}{unit}" if unit == "B" else f"{n:.0f}{unit}"
        n //= 1024
    return f"{n}G"


class CodeTree(Tool):
    name = "code.tree"
    description = (
        "Render a compact, depth-bounded directory tree (folders and files with "
        "sizes), skipping noise dirs like .git/node_modules/venvs. One cheap call "
        "to orient at the start of a task instead of many fs.list calls. Never "
        "returns file contents."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to map. Defaults to first allowed root."},
            "max_depth": {"type": "integer", "default": 3, "minimum": 1, "maximum": 8},
            "max_entries": {"type": "integer", "default": 400, "minimum": 10, "maximum": 2000},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            root = _resolve(ctx, args.get("path"))
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
        if not root.is_dir():
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"{root} is not a directory")

        max_depth = int(args.get("max_depth", 3))
        max_entries = int(args.get("max_entries", 400))
        lines: list[str] = [root.name + "/"]
        count = {"n": 0}
        truncated = {"v": False}

        def walk(d: Path, depth: int, prefix: str):
            if depth > max_depth or truncated["v"]:
                return
            try:
                entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            except OSError:
                return
            entries = [e for e in entries if e.name not in _SKIP_DIRS]
            for i, e in enumerate(entries):
                if count["n"] >= max_entries:
                    truncated["v"] = True
                    return
                last = (i == len(entries) - 1)
                branch = "└── " if last else "├── "
                if e.is_dir():
                    lines.append(f"{prefix}{branch}{e.name}/")
                    count["n"] += 1
                    walk(e, depth + 1, prefix + ("    " if last else "│   "))
                else:
                    try:
                        size = _human(e.stat().st_size)
                    except OSError:
                        size = "?"
                    lines.append(f"{prefix}{branch}{e.name} ({size})")
                    count["n"] += 1

        walk(root, 1, "")
        return ToolResult(status="ok", result={
            "root": str(root), "entries": count["n"],
            "truncated": truncated["v"], "tree": "\n".join(lines),
        }, tool_name=self.name)
