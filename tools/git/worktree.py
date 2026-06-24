"""git.worktree — isolated checkouts so parallel agents don't collide.

JayNet spawns bounded sub-agents (agent.spawn). If two of them — or one
experimenting while main stays clean — share a single working tree, their edits,
branch switches and builds stomp on each other. A git worktree gives each its own
directory backed by the same repo/object store, on its own branch. Typical flow:

    git.worktree add  path=<repo>.worktrees/feat-x  branch=feat-x  create_branch=true
    # point a spawned agent's fs/code.run at that path; it builds + tests there
    git.worktree remove path=<repo>.worktrees/feat-x         # when merged/discarded

Verbs via `action`: list | add | remove | prune. Reuses the confinement +
subprocess helpers from git.status. Mutating actions are confirmation-gated to
match the rest of the git namespace; `remove force=true` can drop uncommitted
work, so it's deliberately explicit.
"""

from __future__ import annotations

from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.git.status import _cfg, _git, _resolve_repo


def _check_dest(ctx: ToolContext, dest: Path) -> None:
    """Bound a worktree destination to tools.git.allowed_roots, if configured."""
    roots = _cfg(ctx).get("allowed_roots")
    if not roots:
        return  # unrestricted, consistent with the rest of the git namespace
    dest = dest.resolve()
    ok = any(dest == Path(r).expanduser().resolve()
             or Path(r).expanduser().resolve() in dest.parents for r in roots)
    if not ok:
        raise PermissionError(
            f"worktree path {dest} is outside tools.git.allowed_roots")


class GitWorktree(Tool):
    name = "git.worktree"
    description = (
        "Manage git worktrees — isolated checkouts of one repo in separate "
        "directories. Use to give a spawned sub-agent (or a risky experiment) its "
        "own working tree on its own branch so parallel work doesn't collide. "
        "action: list | add | remove | prune."
    )
    private = True
    requires_confirmation = True   # add/remove/prune change on-disk state
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Source repo. Defaults to tools.git.default_repo."},
            "action": {"type": "string", "enum": ["list", "add", "remove", "prune"],
                       "default": "list"},
            "path": {"type": "string",
                     "description": "Worktree directory (for add/remove)."},
            "branch": {"type": "string",
                       "description": "Branch to check out (add). With create_branch, a new branch."},
            "create_branch": {"type": "boolean", "default": False,
                              "description": "add: create the branch (git worktree add -b)."},
            "force": {"type": "boolean", "default": False,
                      "description": "remove: drop the worktree even with uncommitted changes."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
        except PermissionError as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))

        action = args.get("action", "list")

        if action == "list":
            rc, out, err = await _git(repo, "worktree", "list", "--porcelain")
            if rc != 0:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=err.strip() or f"git rc {rc}")
            trees, cur = [], {}
            for line in out.splitlines():
                if not line.strip():
                    if cur:
                        trees.append(cur); cur = {}
                    continue
                key, _, val = line.partition(" ")
                if key == "worktree":
                    cur["path"] = val
                elif key == "HEAD":
                    cur["head"] = val
                elif key == "branch":
                    cur["branch"] = val.replace("refs/heads/", "")
                elif key in ("bare", "detached"):
                    cur[key] = True
            if cur:
                trees.append(cur)
            return ToolResult(status="ok", result={"repo": str(repo), "worktrees": trees},
                              tool_name=self.name)

        if action == "prune":
            rc, out, err = await _git(repo, "worktree", "prune", "-v")
            if rc != 0:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=err.strip() or f"git rc {rc}")
            return ToolResult(status="ok", result={
                "repo": str(repo), "pruned": (out or err).strip() or "nothing to prune",
            }, tool_name=self.name)

        # add / remove both need a path
        path = args.get("path")
        if not path:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"action={action} requires 'path'")
        dest = Path(path).expanduser()
        try:
            _check_dest(ctx, dest)
        except PermissionError as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))

        if action == "add":
            git_args = ["worktree", "add"]
            if args.get("create_branch") and args.get("branch"):
                git_args += ["-b", args["branch"], str(dest)]
            else:
                git_args += [str(dest)]
                if args.get("branch"):
                    git_args.append(args["branch"])
            rc, out, err = await _git(repo, *git_args, timeout=60)
            if rc != 0:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=(err.strip() or out.strip() or f"git rc {rc}"))
            return ToolResult(status="ok", result={
                "repo": str(repo), "worktree": str(dest.resolve()),
                "branch": args.get("branch"),
                "created_branch": bool(args.get("create_branch") and args.get("branch")),
                "hint": "point a spawned agent's cwd/fs at this path for isolated work",
            }, tool_name=self.name)

        # action == remove
        git_args = ["worktree", "remove"]
        if args.get("force"):
            git_args.append("--force")
        git_args.append(str(dest))
        rc, out, err = await _git(repo, *git_args)
        if rc != 0:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=(err.strip() or out.strip() or f"git rc {rc}"))
        return ToolResult(status="ok", result={
            "repo": str(repo), "removed": str(dest), "forced": bool(args.get("force")),
        }, tool_name=self.name)
