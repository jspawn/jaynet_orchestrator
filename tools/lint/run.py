"""lint.run — fast static feedback (lint / format-check / type-check).

`test.run` is the right tool for behaviour, but it's confirmation-gated and
relatively heavy. For the tight inner loop the agent wants sub-second signal:
"is this syntactically sane / typed / formatted before I bother running it?"
`lint.run` wraps the common tools and returns a compact pass/fail with a bounded
list of findings — no approval gate, so it's cheap to call after every edit.

Linters are looked up by a small registry (config-extensible via tools.lint.tools)
and only run if their binary is actually present; missing ones are reported, not
fatal. Read-only by default. `fix=true` lets formatters/auto-fixers rewrite files
(confined to the allowed roots) — still no confirmation because it's bounded to a
project path and the changes are reviewable with git.diff.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult, resolve_in_roots, work_roots

# name -> {check: [...argv], fix: [...argv] or None}. {path} is substituted.
# Only entries whose first binary exists are offered.
_LINTERS = {
    "ruff":   {"check": ["ruff", "check", "{path}"],
               "fix":   ["ruff", "check", "--fix", "{path}"]},
    "ruff-format": {"check": ["ruff", "format", "--check", "{path}"],
                    "fix":   ["ruff", "format", "{path}"]},
    "mypy":   {"check": ["mypy", "{path}"], "fix": None},
    "pyflakes": {"check": ["pyflakes", "{path}"], "fix": None},
    "black":  {"check": ["black", "--check", "{path}"],
               "fix":   ["black", "{path}"]},
    "eslint": {"check": ["eslint", "{path}"],
               "fix":   ["eslint", "--fix", "{path}"]},
    "tsc":    {"check": ["tsc", "--noEmit"], "fix": None},
    "shellcheck": {"check": ["shellcheck", "{path}"], "fix": None},
}


def _cfg(ctx: ToolContext) -> dict:
    return ctx.config.get("tools", {}).get("lint", {}) or {}


def _allowed_roots(ctx: ToolContext) -> list[Path]:
    return work_roots(ctx)


def _resolve(ctx: ToolContext, path: str | None) -> Path:
    # Relative paths anchor to the work_root (via resolve_in_roots), not the
    # process CWD — so a bare 'path' lands in the workspace, no probing.
    return resolve_in_roots(work_roots(ctx), path or ".", must_exist=True)


async def _run(argv: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"timed out after {timeout}s"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _tail(text: str, n: int) -> tuple[str, bool]:
    lines = text.splitlines()
    if len(lines) <= n:
        return text, False
    return "\n".join(lines[-n:]), True


class LintRun(Tool):
    name = "lint.run"
    description = (
        "Run linters / format-checkers / type-checkers on a path and get a compact "
        "pass/fail with findings — fast feedback after an edit, before paying for a "
        "full test.run. Auto-detects which tools are installed (ruff, mypy, black, "
        "eslint, tsc, shellcheck...). Set fix=true to let formatters auto-fix in "
        "place (review with git.diff)."
    )
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "File or directory to lint. Defaults to first allowed root."},
            "linters": {"type": "array", "items": {"type": "string"},
                        "description": "Which linters to run (e.g. ['ruff','mypy']). "
                                       "Omit to run all installed ones that apply."},
            "fix": {"type": "boolean", "default": False,
                    "description": "Apply auto-fixes/formatting in place where supported."},
            "max_output_lines": {"type": "integer", "default": 120, "minimum": 10, "maximum": 1000},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            path = _resolve(ctx, args.get("path"))
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))

        cfg = _cfg(ctx)
        registry = dict(_LINTERS)
        registry.update(cfg.get("tools") or {})       # operator extensions/overrides
        timeout = int(cfg.get("timeout_s", 120))
        fix = bool(args.get("fix"))
        max_lines = int(args.get("max_output_lines", 120))

        requested = args.get("linters")
        names = list(requested) if requested else list(registry.keys())

        cwd = path if path.is_dir() else path.parent
        results = []
        missing = []
        passed = True
        for name in names:
            spec = registry.get(name)
            if not spec:
                missing.append(f"{name} (unknown)")
                continue
            argv_tmpl = spec.get("fix") if (fix and spec.get("fix")) else spec.get("check")
            if not argv_tmpl:
                continue
            if not shutil.which(argv_tmpl[0]):
                missing.append(name)
                continue
            argv = [a.format(path=str(path)) for a in argv_tmpl]
            rc, out, err = await _run(argv, cwd, timeout)
            combined, truncated = _tail((out + ("\n" + err if err else "")).strip(), max_lines)
            ok = (rc == 0)
            passed = passed and ok
            results.append({
                "linter": name, "ok": ok, "exit_code": rc,
                "mode": "fix" if (fix and spec.get("fix")) else "check",
                "output": combined, "truncated": truncated,
            })

        if not results:
            return ToolResult(status="ok", result={
                "path": str(path), "passed": None,
                "note": "no applicable linters were installed",
                "missing": missing,
            }, tool_name=self.name)

        return ToolResult(status="ok", result={
            "path": str(path), "passed": passed,
            "ran": [r["linter"] for r in results],
            "missing": missing,
            "results": results,
        }, tool_name=self.name)
