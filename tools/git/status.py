"""Git tools — let the agent checkpoint its own work and reason over history.

Read ops (status/diff/log/show) plus a small set of write ops (add/commit/branch).
Diffs and logs are bounded so they don't blow the orchestrator's context window.

Marked private: a diff of your code is proprietary content, so by default it
will NOT be forwarded to cloud LLM tools. Re-run with --share-private when you
explicitly want Claude/Gemini to look at a diff.

All ops are confined to tools.git.allowed_roots (if set), and every op resolves
to a real git work tree first.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult


def _cfg(ctx: ToolContext) -> dict:
    return ctx.config.get("tools", {}).get("git", {})


def _resolve_repo(ctx: ToolContext, repo: str | None) -> Path:
    cfg = _cfg(ctx)
    path = Path(repo or cfg.get("default_repo") or ".").expanduser().resolve()
    roots = cfg.get("allowed_roots")
    if roots:
        ok = any(str(path).startswith(str(Path(r).expanduser().resolve())) for r in roots)
        if not ok:
            raise PermissionError(f"repo {path} is outside tools.git.allowed_roots")
    if not (path / ".git").exists():
        # Could be a subdir of a repo or a worktree; let git decide.
        pass
    return path


async def _git(repo: Path, *git_args: str, timeout: int = 30) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *git_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"git timed out after {timeout}s"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _bounded(text: str, max_lines: int) -> tuple[str, bool]:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    return "\n".join(lines[:max_lines]), True


class GitStatus(Tool):
    name = "git.status"
    description = ("Show working-tree status: current branch, ahead/behind, and "
                  "changed/untracked files (porcelain).")
    private = True
    parameters = {
        "type": "object",
        "properties": {"repo": {"type": "string", "description": "Repo path. "
                                "Defaults to tools.git.default_repo."}},
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
        except PermissionError as e:
            return ToolResult(status="error", result=None, error=str(e))
        rc, out, err = await _git(repo, "status", "--porcelain=v1", "--branch")
        if rc != 0:
            return ToolResult(status="error", result=None, error=err.strip() or f"git rc {rc}")
        return ToolResult(status="ok", result={"repo": str(repo), "status": out.strip()})


class GitDiff(Tool):
    name = "git.diff"
    description = ("Show a diff. By default the unstaged working-tree diff; set "
                  "staged=true for the index, or pass a ref/path. Bounded to "
                  "max_lines to protect context.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "staged": {"type": "boolean", "default": False,
                       "description": "Diff the staged index instead of the work tree."},
            "ref": {"type": "string", "description": "Optional ref or ref range, e.g. "
                    "'HEAD~3' or 'main..HEAD'."},
            "path": {"type": "string", "description": "Limit the diff to this path."},
            "max_lines": {"type": "integer", "default": 400, "minimum": 10, "maximum": 4000},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
        except PermissionError as e:
            return ToolResult(status="error", result=None, error=str(e))
        git_args = ["diff", "--no-color"]
        if args.get("staged"):
            git_args.append("--cached")
        if args.get("ref"):
            git_args.append(args["ref"])
        if args.get("path"):
            git_args += ["--", args["path"]]
        rc, out, err = await _git(repo, *git_args)
        if rc != 0:
            return ToolResult(status="error", result=None, error=err.strip() or f"git rc {rc}")
        body, truncated = _bounded(out, int(args.get("max_lines", 400)))
        return ToolResult(status="ok", result={
            "repo": str(repo), "diff": body, "truncated": truncated,
        })


class GitLog(Tool):
    name = "git.log"
    description = "Show recent commits (hash, author, date, subject)."
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "n": {"type": "integer", "default": 15, "minimum": 1, "maximum": 200},
            "path": {"type": "string", "description": "Only commits touching this path."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
        except PermissionError as e:
            return ToolResult(status="error", result=None, error=str(e))
        fmt = "%h%x09%an%x09%ad%x09%s"
        git_args = ["log", f"-n{int(args.get('n', 15))}", "--date=short", f"--pretty={fmt}"]
        if args.get("path"):
            git_args += ["--", args["path"]]
        rc, out, err = await _git(repo, *git_args)
        if rc != 0:
            return ToolResult(status="error", result=None, error=err.strip() or f"git rc {rc}")
        commits = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 4:
                commits.append({"hash": parts[0], "author": parts[1],
                                "date": parts[2], "subject": parts[3]})
        return ToolResult(status="ok", result={"repo": str(repo), "commits": commits})


class GitShow(Tool):
    name = "git.show"
    description = "Show a single commit: metadata plus its diff (bounded)."
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "ref": {"type": "string", "default": "HEAD",
                    "description": "Commit ref to show."},
            "max_lines": {"type": "integer", "default": 400, "minimum": 10, "maximum": 4000},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
        except PermissionError as e:
            return ToolResult(status="error", result=None, error=str(e))
        rc, out, err = await _git(repo, "show", "--no-color", args.get("ref", "HEAD"))
        if rc != 0:
            return ToolResult(status="error", result=None, error=err.strip() or f"git rc {rc}")
        body, truncated = _bounded(out, int(args.get("max_lines", 400)))
        return ToolResult(status="ok", result={"repo": str(repo), "show": body,
                                                "truncated": truncated})


class GitAdd(Tool):
    name = "git.add"
    description = "Stage one or more paths (or '.' for everything)."
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Paths to stage. Use ['.'] for all."},
        },
        "required": ["paths"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
        except PermissionError as e:
            return ToolResult(status="error", result=None, error=str(e))
        paths = args["paths"] or ["."]
        rc, out, err = await _git(repo, "add", "--", *paths)
        if rc != 0:
            return ToolResult(status="error", result=None, error=err.strip() or f"git rc {rc}")
        return ToolResult(status="ok", result={"repo": str(repo), "staged": paths})


class GitCommit(Tool):
    name = "git.commit"
    description = ("Create a commit from the staged index with the given message. "
                  "Stage first with git.add (or set add_all=true).")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "message": {"type": "string", "description": "Commit message."},
            "add_all": {"type": "boolean", "default": False,
                        "description": "git add -A before committing."},
        },
        "required": ["message"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
        except PermissionError as e:
            return ToolResult(status="error", result=None, error=str(e))
        if args.get("add_all"):
            rc, _, err = await _git(repo, "add", "-A")
            if rc != 0:
                return ToolResult(status="error", result=None, error=err.strip())
        rc, out, err = await _git(repo, "commit", "-m", args["message"])
        if rc != 0:
            # 'nothing to commit' shows on stdout, not stderr
            return ToolResult(status="error", result=None,
                              error=(err.strip() or out.strip() or f"git rc {rc}"))
        return ToolResult(status="ok", result={"repo": str(repo), "output": out.strip()})


class GitBranch(Tool):
    name = "git.branch"
    description = ("List branches, or create/switch a branch. With no name: list. "
                  "With name + create=true: create and switch. With name only: switch.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "name": {"type": "string", "description": "Branch to create/switch to. "
                     "Omit to list branches."},
            "create": {"type": "boolean", "default": False},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
        except PermissionError as e:
            return ToolResult(status="error", result=None, error=str(e))
        name = args.get("name")
        if not name:
            rc, out, err = await _git(repo, "branch", "--list", "--no-color")
            if rc != 0:
                return ToolResult(status="error", result=None, error=err.strip())
            branches = [b.strip("* ").strip() for b in out.splitlines() if b.strip()]
            return ToolResult(status="ok", result={"repo": str(repo), "branches": branches})
        git_args = ["switch", "-c", name] if args.get("create") else ["switch", name]
        rc, out, err = await _git(repo, *git_args)
        if rc != 0:
            return ToolResult(status="error", result=None, error=err.strip() or f"git rc {rc}")
        return ToolResult(status="ok", result={"repo": str(repo),
                                               "switched_to": name,
                                               "created": bool(args.get("create"))})
