"""Code execution tool.

Runs short Python snippets in a sandboxed subprocess. Default sandbox is firejail
(must be installed: `sudo pacman -S firejail`). Falls back to plain subprocess if
firejail is unavailable, but that's NOT recommended on a multi-user box.

The tool is intentionally limited — for heavy computation, build a dedicated
service. This is for things like math, JSON manipulation, regex tests, quick
unit-conversion, parsing.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import textwrap
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult


_PREAMBLE = """\
# Auto-injected preamble: bounded imports, no network, no fs writes outside cwd
import sys, os
sys.path = [p for p in sys.path if p and not p.startswith('/home')]
"""


class CodeExecute(Tool):
    name = "code.execute"
    description = (
        "Execute a short Python snippet and return stdout. Sandboxed; "
        "no network, limited imports, 30s timeout. Use for math, JSON manipulation, "
        "regex tests, quick computations. Output must be printed to stdout."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source. Use print() to return values.",
            },
            "timeout_s": {
                "type": "integer", "default": 30, "minimum": 1, "maximum": 60,
            },
        },
        "required": ["code"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        code = args["code"]
        cfg = ctx.config.get("tools", {}).get("code", {})
        timeout = min(int(args.get("timeout_s", cfg.get("timeout_s", 30))), 60)
        sandbox = cfg.get("sandbox", "firejail")
        # Per-CALL sandbox dir under the configured base. Deliberately NOT the
        # run's /tmp tmp_root: firejail runs with --private-tmp, which mounts a
        # fresh /tmp and would HIDE any workdir living under /tmp (that was the
        # exit-1 regression). The base must be a real, non-/tmp path. A unique
        # dir per call means concurrent (parallel) executes don't clobber each
        # other, and it's removed in `finally`, so each call is self-cleaning.
        from runtime.paths import SANDBOX_DIR
        base = Path(cfg.get("workdir", str(SANDBOX_DIR)))
        base.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="exec-", dir=base))

        full_source = _PREAMBLE + "\n" + textwrap.dedent(code)

        with tempfile.NamedTemporaryFile(
                "w", suffix=".py", dir=workdir, delete=False) as f:
            f.write(full_source)
            script = f.name

        try:
            cmd = self._build_cmd(script, sandbox, workdir)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(status="error", result=None,
                                  error=f"execution timeout after {timeout}s")

            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                return ToolResult(status="error", result={
                    "stdout": out[-2000:], "stderr": err[-2000:],
                    "exit_code": proc.returncode,
                }, error=f"exit code {proc.returncode}")

            return ToolResult(status="ok", result={
                "stdout": out[-5000:],
                "stderr": err[-1000:] if err else "",
                "exit_code": 0,
            })
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _build_cmd(self, script: str, sandbox: str | None, workdir: Path) -> list[str]:
        python = "python"
        if sandbox == "firejail" and shutil.which("firejail"):
            return [
                "firejail",
                "--quiet",
                "--noprofile",
                "--net=none",                       # no network
                "--private-tmp",
                f"--private-cwd={workdir}",
                "--read-only=/",
                f"--read-write={workdir}",
                "--rlimit-as=1073741824",           # 1 GB virtual memory cap
                "--rlimit-cpu=60",
                python, script,
            ]
        # Fallback: no sandbox. Logged but allowed for dev convenience.
        import logging
        logging.warning("firejail unavailable — running code.execute WITHOUT sandbox")
        return [python, script]
