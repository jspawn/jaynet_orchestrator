"""code.patch — apply a unified diff to files under the allowed roots.

`fs.edit` does a single unique-string replacement — perfect for one small, exact
change, but awkward for a multi-hunk edit (you'd call it repeatedly, and each call
risks a non-unique match). `code.patch` takes a standard unified diff (the format
`git diff` / `diff -u` produce) and applies it atomically via `git apply`, so the
agent can land a coherent multi-file, multi-hunk change in one step without
re-reading whole files back into context.

It uses `git apply` even outside a git repo (`git apply` works on a plain tree),
which gives robust fuzz handling, a real dry-run (`--check`), and clean rejection
when the base doesn't match — far safer than hand-rolling patch logic. Paths in
the diff are resolved against `base_dir` and every target must fall inside the
allowed roots. Mutating, so it is private + confirmation-gated like fs.write.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult


def _allowed_roots(ctx: ToolContext) -> list[Path]:
    roots = (ctx.config.get("tools", {}).get("fs", {}) or {}).get(
        "allowed_roots") or ["/srv/orchestrator/data"]
    return [Path(r).expanduser().resolve() for r in roots]


def _resolve_base(ctx: ToolContext, base_dir: str | None) -> Path:
    roots = _allowed_roots(ctx)
    p = Path(base_dir or roots[0]).expanduser().resolve()
    if not any(p == r or r in p.parents for r in roots):
        allowed = ", ".join(str(r) for r in roots)
        raise PermissionError(f"base_dir {p} is outside the allowed roots ({allowed}).")
    if not p.exists():
        raise FileNotFoundError(f"base_dir does not exist: {p}")
    return p


# Pull target paths out of a unified diff so we can bounds-check them before
# touching anything. Handles 'diff --git a/x b/x', '--- a/x', '+++ b/x'.
_HUNK_PATHS = re.compile(r'^(?:diff --git a/(\S+) b/(\S+)|[-+]{3} (?:[ab]/)?(\S+))', re.M)


def _targets(diff: str) -> list[str]:
    seen: list[str] = []
    for m in _HUNK_PATHS.finditer(diff):
        for g in m.groups():
            if g and g != "/dev/null" and g not in seen:
                seen.append(g)
    return seen


async def _git_apply(base: Path, diff_path: Path, *flags: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(base), "apply", *flags, str(diff_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class CodePatch(Tool):
    name = "code.patch"
    description = (
        "Apply a unified diff (git diff / diff -u format) to files under a base "
        "directory — a coherent multi-hunk, multi-file edit in one atomic step. "
        "Prefer this over repeated fs.edit calls for larger changes. Use dry_run "
        "to validate the patch applies cleanly before committing to it."
    )
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "diff": {
                "type": "string",
                "description": "The unified diff text. Paths may be prefixed a/ b/.",
            },
            "base_dir": {
                "type": "string",
                "description": "Directory the diff paths are relative to. Must be "
                               "inside the allowed roots. Defaults to the first root.",
            },
            "strip": {
                "type": "integer", "default": 1, "minimum": 0, "maximum": 4,
                "description": "Leading path components to strip (git apply -p). "
                               "1 matches a/ b/ prefixes; use 0 for bare paths.",
            },
            "dry_run": {
                "type": "boolean", "default": False,
                "description": "Only check the patch applies; don't modify files.",
            },
        },
        "required": ["diff"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            base = _resolve_base(ctx, args.get("base_dir"))
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))

        diff = args["diff"]
        if not diff.strip():
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="empty diff")
        strip = int(args.get("strip", 1))

        # Bounds-check every target path against the allowed roots first.
        roots = _allowed_roots(ctx)
        targets = _targets(diff)
        for rel in targets:
            tp = (base / rel).resolve()
            if not any(tp == r or r in tp.parents for r in roots):
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"patch targets {tp}, outside the allowed roots")

        # Write the diff to a temp file (git apply reads a file cleanly; avoids
        # any stdin quoting surprises with multi-line content).
        fd, tmp = tempfile.mkstemp(suffix=".diff", prefix="orch-patch-")
        os.close(fd)
        Path(tmp).write_text(diff if diff.endswith("\n") else diff + "\n")
        try:
            pflag = f"-p{strip}"
            # Always check first so we never half-apply.
            rc, out, err = await _git_apply(base, Path(tmp), "--check", "--verbose", pflag)
            if rc != 0:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=(f"patch does not apply cleanly: "
                                         f"{(err or out).strip()[:1500]}"))
            if args.get("dry_run"):
                return ToolResult(status="ok", result={
                    "applied": False, "dry_run": True, "base_dir": str(base),
                    "files": targets, "message": "patch applies cleanly",
                }, tool_name=self.name)

            rc, out, err = await _git_apply(base, Path(tmp), pflag)
            if rc != 0:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"git apply failed: {(err or out).strip()[:1500]}")
            return ToolResult(status="ok", result={
                "applied": True, "base_dir": str(base), "files": targets,
                "message": f"applied {len(targets)} file(s)",
            }, tool_name=self.name)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
