"""Git remote + working-tree ops — round out the git namespace for a real loop.

status.py covers read ops + add/commit/branch. This adds the rest a coding agent
needs to actually collaborate with a remote (the user keeps repos on a Synology
NAS) and to manage the working tree:

- git.fetch   — update remote-tracking refs (read-only; safe, no confirmation)
- git.pull    — fetch + integrate (touches the work tree; confirmation)
- git.push    — publish commits to a remote (leaves the box; confirmation)
- git.stash   — save / pop / list / drop local changes (confirmation on mutating)
- git.restore — discard working-tree changes to path(s) (destructive; confirmation)

All reuse the confinement + subprocess helpers from git.status so behaviour
(allowed_roots, default_repo, bounded timeouts) stays identical.
"""

from __future__ import annotations

from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.git.status import _check_ref, _git, _resolve_repo  # shared helpers


async def _resolve_remote(repo: Path, value: str | None) -> str:
    """The remote argument must be a NAME configured in the repo, never a URL.

    `git fetch <url>` makes an ungated outbound connection to anything the
    caller hands over, and on some git builds an `ext::` transport in that
    slot is straight command execution — so reject URL-shaped values outright
    and then require the name to appear in `git remote`."""
    remote = value or "origin"
    if ("::" in remote or "://" in remote
            or 0 < remote.find("@") < remote.find(":")):   # scp-like user@host:path
        raise ValueError(f"unsafe remote value: {remote!r}")
    rc, out, _ = await _git(repo, "remote")
    names = {l.strip() for l in out.splitlines() if l.strip()} if rc == 0 else set()
    if remote not in names:
        have = ", ".join(sorted(names)) or "none configured"
        raise ValueError(f"unknown remote {remote!r} (have: {have})")
    return remote


class GitFetch(Tool):
    name = "git.fetch"
    description = ("Update remote-tracking refs from a remote without changing the "
                  "working tree. Read-only and safe; run before pull/rebase decisions.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "remote": {"type": "string", "default": "origin"},
            "prune": {"type": "boolean", "default": False,
                      "description": "Delete local refs for branches gone on the remote."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
            remote = await _resolve_remote(repo, args.get("remote"))
        except (PermissionError, ValueError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
        git_args = ["fetch", remote]
        if args.get("prune"):
            git_args.append("--prune")
        rc, out, err = await _git(repo, *git_args, timeout=120)
        if rc != 0:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=err.strip() or f"git rc {rc}")
        return ToolResult(status="ok", result={
            "repo": str(repo), "output": (err or out).strip() or "up to date",
        }, tool_name=self.name)


class GitPull(Tool):
    name = "git.pull"
    description = ("Fetch and integrate changes from a remote into the current "
                  "branch. Touches the working tree; may conflict. Prefer --ff-only "
                  "(default) so it refuses rather than creating surprise merges.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "remote": {"type": "string", "default": "origin"},
            "branch": {"type": "string", "description": "Branch to pull; defaults to tracking."},
            "ff_only": {"type": "boolean", "default": True,
                        "description": "Only fast-forward; refuse if a merge would be needed."},
            "rebase": {"type": "boolean", "default": False,
                       "description": "Rebase local commits on top instead of merging."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
            remote = (await _resolve_remote(repo, args["remote"])
                      if args.get("remote") else None)
            _check_ref(args.get("branch"))
        except (PermissionError, ValueError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
        git_args = ["pull"]
        if args.get("rebase"):
            git_args.append("--rebase")
        elif args.get("ff_only", True):
            git_args.append("--ff-only")
        if remote:
            git_args.append(remote)
            if args.get("branch"):
                git_args.append(args["branch"])
        rc, out, err = await _git(repo, *git_args, timeout=120)
        if rc != 0:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=(err.strip() or out.strip() or f"git rc {rc}"))
        return ToolResult(status="ok", result={
            "repo": str(repo), "output": (out or err).strip(),
        }, tool_name=self.name)


class GitPush(Tool):
    name = "git.push"
    description = ("Publish local commits to a remote. Sends data off-box, so it is "
                  "confirmation-gated. Use set_upstream=true the first time you push "
                  "a new branch.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "remote": {"type": "string", "default": "origin"},
            "branch": {"type": "string", "description": "Branch to push; defaults to current."},
            "set_upstream": {"type": "boolean", "default": False,
                             "description": "git push -u: set the upstream tracking ref."},
            "force_with_lease": {"type": "boolean", "default": False,
                                 "description": "Safer force-push (refuses if remote moved unexpectedly)."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
            remote = await _resolve_remote(repo, args.get("remote"))
            _check_ref(args.get("branch"))
        except (PermissionError, ValueError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
        git_args = ["push"]
        if args.get("set_upstream"):
            git_args.append("-u")
        if args.get("force_with_lease"):
            git_args.append("--force-with-lease")
        git_args.append(remote)
        if args.get("branch"):
            git_args.append(args["branch"])
        rc, out, err = await _git(repo, *git_args, timeout=120)
        if rc != 0:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=(err.strip() or out.strip() or f"git rc {rc}"))
        # git push reports progress on stderr even on success.
        return ToolResult(status="ok", result={
            "repo": str(repo), "output": (err or out).strip() or "pushed",
        }, tool_name=self.name)


class GitStash(Tool):
    name = "git.stash"
    description = ("Save / pop / list / drop uncommitted changes. action=push saves "
                  "and cleans the work tree; pop re-applies the latest; list shows "
                  "the stack. Use to park work before switching branches.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "action": {"type": "string", "enum": ["push", "pop", "list", "drop"],
                       "default": "push"},
            "message": {"type": "string", "description": "Label for action=push."},
            "include_untracked": {"type": "boolean", "default": False},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
        except PermissionError as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
        action = args.get("action", "push")
        if action == "list":
            rc, out, err = await _git(repo, "stash", "list")
            if rc != 0:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=err.strip() or f"git rc {rc}")
            entries = [l for l in out.splitlines() if l.strip()]
            return ToolResult(status="ok", result={"repo": str(repo), "stashes": entries},
                              tool_name=self.name)
        git_args = ["stash", action]
        if action == "push":
            if args.get("include_untracked"):
                git_args.append("--include-untracked")
            if args.get("message"):
                git_args += ["-m", args["message"]]
        rc, out, err = await _git(repo, *git_args)
        if rc != 0:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=(err.strip() or out.strip() or f"git rc {rc}"))
        return ToolResult(status="ok", result={
            "repo": str(repo), "action": action, "output": (out or err).strip(),
        }, tool_name=self.name)


class GitRestore(Tool):
    name = "git.restore"
    description = ("Discard working-tree changes to one or more paths (git restore), "
                  "or unstage them (staged=true). DESTRUCTIVE: dropped edits are not "
                  "recoverable — confirmation-gated. Stash first if unsure.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Paths to restore. Use ['.'] for the whole tree."},
            "staged": {"type": "boolean", "default": False,
                       "description": "Unstage (restore --staged) instead of discarding edits."},
            "source": {"type": "string",
                       "description": "Restore from this ref instead of the index/HEAD."},
        },
        "required": ["paths"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
        except PermissionError as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
        paths = args.get("paths") or []
        if not paths:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="paths is required")
        git_args = ["restore"]
        if args.get("staged"):
            git_args.append("--staged")
        if args.get("source"):
            git_args += ["--source", args["source"]]
        git_args += ["--", *paths]
        rc, out, err = await _git(repo, *git_args)
        if rc != 0:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=(err.strip() or out.strip() or f"git rc {rc}"))
        return ToolResult(status="ok", result={
            "repo": str(repo), "restored": paths, "staged": bool(args.get("staged")),
        }, tool_name=self.name)
