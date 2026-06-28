"""code.deps — manage a project's Python virtualenv and dependencies.

A coding agent that can run tests also needs to install what those tests import.
This creates/uses a per-project venv (under the project dir, inside the allowed
roots) and installs packages into it — preferring `uv` when present (fast), else
`python -m venv` + `pip`. It deliberately does NOT touch any system or
orchestrator interpreter; it only ever writes a venv beneath the given project
dir. Reaches the network and runs package setup code, so it is private +
confirmation-gated like job.start.

Verbs folded into one tool via `action`: create | install | list.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult, work_roots


def _allowed_roots(ctx: ToolContext) -> list[Path]:
    return work_roots(ctx)


def _resolve_project(ctx: ToolContext, project_dir: str | None) -> Path:
    roots = _allowed_roots(ctx)
    p = Path(project_dir or roots[0]).expanduser().resolve()
    if not any(p == r or r in p.parents for r in roots):
        allowed = ", ".join(str(r) for r in roots)
        raise PermissionError(f"project_dir {p} is outside the allowed roots ({allowed}).")
    return p


async def _run(argv: list[str], cwd: Path, timeout: int = 300) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"timed out after {timeout}s"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _tail(text: str, n: int = 60) -> str:
    return "\n".join(text.splitlines()[-n:])


class CodeDeps(Tool):
    name = "code.deps"
    description = (
        "Manage a project's Python venv and dependencies (action: create | install "
        "| list). Creates/uses a venv under the project dir and installs packages "
        "into it (uv if available, else pip) so code.run/test.run can import them. "
        "Never touches system interpreters."
    )
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "install", "list"],
                       "default": "install"},
            "project_dir": {"type": "string",
                            "description": "Project root (must be under allowed roots)."},
            "packages": {"type": "array", "items": {"type": "string"},
                         "description": "For action=install: package specs, e.g. ['httpx', 'pytest>=8']."},
            "requirements": {"type": "string",
                             "description": "For action=install: path to a requirements file (relative to project_dir)."},
            "venv_name": {"type": "string", "default": ".venv",
                          "description": "Venv directory name under the project."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            project = _resolve_project(ctx, args.get("project_dir"))
        except PermissionError as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))
        if not project.exists():
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"project_dir does not exist: {project}")

        action = args.get("action", "install")
        venv = (project / args.get("venv_name", ".venv")).resolve()
        # Keep the venv strictly inside the project.
        if project not in venv.parents and venv != project:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="venv_name must resolve inside the project dir")
        uv = shutil.which("uv")
        py = venv / "bin" / "python"

        if action == "create" or (action == "install" and not py.exists()):
            if uv:
                rc, out, err = await _run([uv, "venv", str(venv)], project)
            else:
                rc, out, err = await _run([sys.executable, "-m", "venv", str(venv)], project)
            if rc != 0:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"venv create failed: {_tail(err or out)}")
            if action == "create":
                return ToolResult(status="ok", result={
                    "action": "create", "venv": str(venv),
                    "python": str(py), "tool": "uv" if uv else "venv",
                }, tool_name=self.name)

        if action == "list":
            if not py.exists():
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"no venv at {venv}; run action=create first")
            rc, out, err = await _run([str(py), "-m", "pip", "list", "--format=freeze"], project)
            if rc != 0:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=_tail(err or out))
            pkgs = [l for l in out.splitlines() if l.strip()]
            return ToolResult(status="ok", result={
                "action": "list", "venv": str(venv), "count": len(pkgs),
                "packages": pkgs[:300],
            }, tool_name=self.name)

        # action == install
        packages = list(args.get("packages") or [])
        req = args.get("requirements")
        if not packages and not req:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="install needs 'packages' or 'requirements'")
        if uv:
            base = [uv, "pip", "install", "--python", str(py)]
        else:
            base = [str(py), "-m", "pip", "install"]
        argv = list(base)
        if req:
            reqp = (project / req).resolve()
            if project not in reqp.parents and reqp != project:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error="requirements path escapes the project dir")
            argv += ["-r", str(reqp)]
        argv += packages
        rc, out, err = await _run(argv, project, timeout=600)
        if rc != 0:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"install failed (rc {rc}): {_tail(err or out)}")
        return ToolResult(status="ok", result={
            "action": "install", "venv": str(venv), "python": str(py),
            "installed": packages, "requirements": req,
            "tool": "uv" if uv else "pip", "log_tail": _tail(out, 25),
        }, tool_name=self.name)
