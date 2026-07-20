"""Code execution tool.

Runs short Python snippets in a sandboxed subprocess. Default sandbox is firejail
(must be installed: `sudo pacman -S firejail`). Falls back to plain subprocess if
firejail is unavailable, but that's NOT recommended on a multi-user box.

Two output channels:
- stdout (what the snippet prints) — the classic path.
- files: anything the snippet writes into the ORCH_EXEC_OUT directory (an env
  var the tool sets when the run has a workspace) SURVIVES the call and is
  returned as written_files — that's how matplotlib charts get out. The
  interpreter is configurable (tools.code.python): point it at the runtime venv
  for numpy/matplotlib (Agg backend is forced), system python is the fallback.

The tool is intentionally limited — for heavy computation, build a dedicated
service. This is for things like math, JSON manipulation, regex tests, quick
unit-conversion, parsing, small plots.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import textwrap
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult, scrub_env


_PREAMBLE = """\
# Auto-injected preamble: bounded imports, no network, no fs writes outside cwd
import sys, os
sys.path = [p for p in sys.path if p and not p.startswith('/home')]
"""


class CodeExecute(Tool):
    name = "code.execute"
    description = (
        "Execute a short Python snippet and return stdout. Sandboxed; "
        "no network, 30s timeout. Use for math, JSON manipulation, regex tests, "
        "quick computations, small plots. numpy/matplotlib are available (Agg "
        "backend): save files to the directory named by the ORCH_EXEC_OUT env "
        "var (os.environ['ORCH_EXEC_OUT']) — they're returned as written_files, "
        "hand them to the user with deliver.files. Print results to stdout."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source. Use print() to return values; "
                               "save files into ORCH_EXEC_OUT to keep them.",
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

        # Artifact dir: files written here SURVIVE the call (charts, CSVs) and
        # are returned for delivery. Lives under the run's workspace so
        # deliver.files can reach it; per-call subdir keeps concurrent executes
        # apart. None without a workspace (CLI path) — stdout only then.
        out_dir = None
        if getattr(ctx, "work_root", None):
            out_dir = Path(ctx.work_root) / "exec-out" / workdir.name
            out_dir.mkdir(parents=True, exist_ok=True)

        full_source = _PREAMBLE + "\n" + textwrap.dedent(code)

        with tempfile.NamedTemporaryFile(
                "w", suffix=".py", dir=workdir, delete=False) as f:
            f.write(full_source)
            script = f.name

        # Secrets stay out of the sandbox; MPLBACKEND keeps matplotlib headless;
        # single BLAS thread keeps numpy's buffer/stack appetite inside the
        # rlimit (and the sandbox from burning all cores).
        env = scrub_env(dict(os.environ))
        env.setdefault("MPLBACKEND", "Agg")
        env.setdefault("OMP_NUM_THREADS", "1")
        env.setdefault("OPENBLAS_NUM_THREADS", "1")
        if out_dir:
            env["ORCH_EXEC_OUT"] = str(out_dir)

        try:
            cmd = self._build_cmd(script, sandbox, workdir, out_dir,
                                  cfg.get("python", "python"),
                                  int(cfg.get("rlimit_as_mb", 1024)))
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                env=env,
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
            artifacts = self._artifacts(out_dir)

            if proc.returncode != 0:
                return ToolResult(status="error", result={
                    "stdout": out[-2000:], "stderr": err[-2000:],
                    "exit_code": proc.returncode,
                    **({"out_dir": str(out_dir), "written_files": artifacts}
                       if out_dir else {}),
                }, error=f"exit code {proc.returncode}")

            return ToolResult(status="ok", result={
                "stdout": out[-5000:],
                "stderr": err[-1000:] if err else "",
                "exit_code": 0,
                **({"out_dir": str(out_dir), "written_files": artifacts}
                   if out_dir else {}),
            })
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _artifacts(out_dir: Path | None) -> list[str]:
        if not out_dir or not out_dir.exists():
            return []
        return sorted(str(p) for p in out_dir.iterdir() if p.is_file())

    def _build_cmd(self, script: str, sandbox: str | None, workdir: Path,
                   out_dir: Path | None, python: str, rlimit_as_mb: int) -> list[str]:
        if sandbox == "firejail" and shutil.which("firejail"):
            cmd = [
                "firejail",
                "--quiet",
                "--noprofile",
                "--net=none",                       # no network
                "--private-tmp",
                f"--private-cwd={workdir}",
                "--read-only=/",
                f"--read-write={workdir}",
            ]
            if out_dir:
                cmd.append(f"--read-write={out_dir}")   # artifact escape hatch
            cmd += [
                f"--rlimit-as={rlimit_as_mb * 1024**2}",  # virtual address space cap
                "--rlimit-cpu=60",
                python, script,
            ]
            return cmd
        # Fallback: no sandbox. Logged but allowed for dev convenience.
        import logging
        logging.warning("firejail unavailable — running code.execute WITHOUT sandbox")
        return [python, script]
