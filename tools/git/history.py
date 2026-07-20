"""Git history + repo-level ops — blame, merge, tag, reset, clone.

Rounds out the namespace beyond status.py (read ops + add/commit/branch) and
remote.py (fetch/pull/push/stash/restore):

- git.blame — per-line authorship of a file (read-only; no confirmation)
- git.merge — integrate a ref into the current branch (confirmation)
- git.tag   — list / create / delete tags (confirmation only for mutations)
- git.reset — soft/mixed/hard reset or unstage paths (confirmation; hard is
              destructive)
- git.clone — clone a repo into the workspace (confirmation; leaves the box
              for remote URLs)

Same confinement + subprocess helpers as the rest of the namespace.
"""

from __future__ import annotations

from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult, work_roots
from tools.git.status import _bounded, _cfg as _git_cfg, _check_ref, _git, _resolve_repo


def _git_roots(ctx: ToolContext) -> list[Path]:
    """Confinement roots for the git namespace: tools.git.allowed_roots, falling
    back to the run's workspace — the same resolution _resolve_repo uses."""
    roots = [Path(r).expanduser().resolve()
             for r in (_git_cfg(ctx).get("allowed_roots") or [])]
    return roots or work_roots(ctx)


class GitBlame(Tool):
    name = "git.blame"
    description = ("Show per-line authorship of a file (who last touched each line, "
                   "which commit). Bounded to max_lines to protect context.")
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "path": {"type": "string", "description": "File to blame (repo-relative)."},
            "start_line": {"type": "integer", "minimum": 1,
                           "description": "First line of a range (with end_line)."},
            "end_line": {"type": "integer", "minimum": 1,
                         "description": "Last line of a range."},
            "max_lines": {"type": "integer", "default": 400, "minimum": 10, "maximum": 4000},
        },
        "required": ["path"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
        except PermissionError as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
        git_args = ["blame"]
        if args.get("start_line"):
            start = int(args["start_line"])
            end = int(args.get("end_line") or start)
            git_args += ["-L", f"{start},{end}"]
        git_args += ["--", args["path"]]
        rc, out, err = await _git(repo, *git_args)
        if rc != 0:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=err.strip() or f"git rc {rc}")
        body, truncated = _bounded(out, int(args.get("max_lines", 400)))
        return ToolResult(status="ok", result={
            "repo": str(repo), "path": args["path"], "blame": body,
            "truncated": truncated}, tool_name=self.name)


class GitMerge(Tool):
    name = "git.merge"
    description = ("Merge a ref into the current branch. May conflict — on conflict "
                   "the result says so and the tree stays in merge state (finish or "
                   "abort). confirmation-gated: rewrites history and the work tree.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "ref": {"type": "string", "description": "Branch/ref to merge in."},
            "no_ff": {"type": "boolean", "default": False,
                      "description": "Force a merge commit even when fast-forward is possible."},
            "ff_only": {"type": "boolean", "default": False,
                        "description": "Refuse unless the merge is a fast-forward."},
            "abort": {"type": "boolean", "default": False,
                      "description": "Abort an in-progress conflicted merge (git merge --abort)."},
            "message": {"type": "string", "description": "Merge commit message (-m)."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
            _check_ref(args.get("ref"))
        except (PermissionError, ValueError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
        if args.get("abort"):
            git_args = ["merge", "--abort"]
        else:
            if not args.get("ref"):
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error="ref is required (or pass abort=true)")
            git_args = ["merge"]
            if args.get("no_ff"):
                git_args.append("--no-ff")
            if args.get("ff_only"):
                git_args.append("--ff-only")
            if args.get("message"):
                git_args += ["-m", args["message"]]
            git_args.append(args["ref"])
        rc, out, err = await _git(repo, *git_args, timeout=60)
        if rc != 0:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=(err.strip() or out.strip() or f"git rc {rc}"))
        return ToolResult(status="ok", result={
            "repo": str(repo), "output": (out or err).strip() or "merged",
        }, tool_name=self.name)


class GitTag(Tool):
    name = "git.tag"
    description = ("List, create, or delete tags. action=list needs no approval; "
                   "create/delete are confirmation-gated. With message, the tag is "
                   "annotated; otherwise lightweight.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "action": {"type": "string", "enum": ["list", "create", "delete"],
                       "default": "list"},
            "name": {"type": "string", "description": "Tag name (create/delete)."},
            "message": {"type": "string", "description": "Annotation message (create)."},
            "ref": {"type": "string", "description": "Commit to tag (create; default HEAD)."},
        },
        "required": [],
    }

    def needs_confirmation(self, args: dict, ctx: ToolContext) -> bool:
        return args.get("action", "list") != "list"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
            _check_ref(args.get("name"))
            _check_ref(args.get("ref"))
        except (PermissionError, ValueError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
        action = args.get("action", "list")
        if action == "list":
            rc, out, err = await _git(repo, "tag", "--list")
            if rc != 0:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=err.strip() or f"git rc {rc}")
            tags = [t for t in (l.strip() for l in out.splitlines()) if t]
            return ToolResult(status="ok", result={"repo": str(repo), "tags": tags},
                              tool_name=self.name)
        name = args.get("name")
        if not name:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"name is required for action={action}")
        if action == "delete":
            git_args = ["tag", "-d", name]
        else:
            git_args = ["tag"]
            if args.get("message"):
                git_args += ["-a", "-m", args["message"]]
            git_args.append(name)
            if args.get("ref"):
                git_args.append(args["ref"])
        rc, out, err = await _git(repo, *git_args)
        if rc != 0:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=(err.strip() or out.strip() or f"git rc {rc}"))
        return ToolResult(status="ok", result={
            "repo": str(repo), "action": action, "name": name,
            "output": (out or err).strip()}, tool_name=self.name)


class GitReset(Tool):
    name = "git.reset"
    description = ("Reset the current branch to a ref (soft/mixed/hard), or unstage "
                   "paths (with paths set, mode is ignored). hard DISCARDS working-tree "
                   "edits — destructive, confirmation-gated; stash first if unsure.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "mode": {"type": "string", "enum": ["soft", "mixed", "hard"], "default": "mixed",
                     "description": "soft: move HEAD only · mixed: + unstage · hard: + discard edits."},
            "ref": {"type": "string", "default": "HEAD",
                    "description": "Ref to reset to (e.g. HEAD~1)."},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Unstage/reset only these paths instead of the branch."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            repo = _resolve_repo(ctx, args.get("repo"))
            _check_ref(args.get("ref", "HEAD"))
        except (PermissionError, ValueError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
        paths = args.get("paths") or []
        if paths:
            git_args = ["reset", args.get("ref", "HEAD"), "--", *paths]
        else:
            git_args = ["reset", f"--{args.get('mode', 'mixed')}", args.get("ref", "HEAD")]
        rc, out, err = await _git(repo, *git_args)
        if rc != 0:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=(err.strip() or out.strip() or f"git rc {rc}"))
        return ToolResult(status="ok", result={
            "repo": str(repo), "output": (out or err).strip() or "reset done",
        }, tool_name=self.name)


class GitClone(Tool):
    name = "git.clone"
    description = ("Clone a repository into the workspace. Remote URLs (https/ssh) "
                   "leave the box, so this is confirmation-gated; local source paths "
                   "must be inside your allowed roots. Use depth=1 for a shallow clone.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "https/ssh/git URL, or a local repo path."},
            "dest": {"type": "string", "description": "Target dir (default: repo name), "
                     "inside your workspace."},
            "depth": {"type": "integer", "minimum": 1,
                      "description": "Shallow clone depth (--depth N)."},
            "branch": {"type": "string", "description": "Clone only this branch (--branch)."},
        },
        "required": ["url"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        url = (args.get("url") or "").strip()
        if not url:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="url is required")
        try:
            _check_ref(args.get("branch"))
        except ValueError as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
        roots = _git_roots(ctx)
        if not roots:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="no workspace to clone into")
        is_remote = url.startswith(("http://", "https://", "git@", "ssh://", "git://"))
        if not is_remote:
            # Local source: must live inside the allowed roots like any other read.
            src = Path(url).expanduser().resolve()
            if not any(src == r or r in src.parents for r in roots):
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"local clone source {src} is outside your workspace")
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        raw = Path(args.get("dest") or name).expanduser()
        dest = (raw if raw.is_absolute() else roots[0] / raw).resolve()
        if not any(dest == r or r in dest.parents for r in roots):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"destination {dest} is outside your workspace")
        git_args = ["clone"]
        if args.get("depth"):
            git_args += ["--depth", str(int(args["depth"]))]
        if args.get("branch"):
            git_args += ["--branch", args["branch"]]
        git_args += [url, str(dest)]
        rc, out, err = await _git(roots[0], *git_args, timeout=300)
        if rc != 0:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=(err.strip() or out.strip() or f"git rc {rc}"))
        return ToolResult(status="ok", result={
            "dest": str(dest), "output": (err or out).strip() or "cloned",
        }, tool_name=self.name)
