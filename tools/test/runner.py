"""Testing harness as an orchestrator capability — `test.run`.

Lets the agent test code the way a careful developer does: write a test (a pytest
module that drives the target in-process via httpx ASGITransport, mocking the
model / external services), run it isolated against a venv that actually has the
test deps, and read structured pass/fail back.

Two modes, one tool:
- quick (default): run pytest as a bounded subprocess and return parsed results
  inline. For fast ASGI/mock checks — the bread and butter.
- detached: hand the same command to the job runner (job.start) so a long suite
  runs in the background; returns a job_id to poll with job.status / job.logs.

SAFETY: this runs arbitrary test code with real dependencies, so it is `private`
and `requires_confirmation`, exactly like job.*. The quick path can be wrapped in
a sandbox via `tools.test.sandbox_prefix` (e.g. firejail); network is left on by
default because ASGITransport needs none but some suites do — keep that in mind.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.job.runner import JobStart


def _cfg(ctx: ToolContext) -> dict:
    return ctx.config.get("tools", {}).get("test", {})


_SAFE = re.compile(r"^[A-Za-z0-9_./-]+$")


def _collect_files(args: dict) -> dict[str, str]:
    files = dict(args.get("files") or {})
    if args.get("test"):
        files.setdefault("test_main.py", args["test"])
    return files


def _validate_paths(files: dict[str, str]) -> str | None:
    for rel in files:
        if rel.startswith("/") or ".." in Path(rel).parts or not _SAFE.match(rel):
            return f"unsafe file path: {rel!r}"
    return None


def _write_files(workdir: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        dest = workdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)


def _pythonpath(ctx: ToolContext, extra: str | None) -> str:
    cfg = _cfg(ctx)
    # Resolve to absolute: the test subprocess runs with cwd=workdir, so a
    # relative project_root would otherwise point at the wrong place.
    parts = [str(Path(cfg.get("project_root", "/srv/orchestrator")).resolve())]
    if cfg.get("extra_pythonpath"):
        parts.append(str(cfg["extra_pythonpath"]))
    if extra:
        parts.append(extra)
    return ":".join(p for p in parts if p)


_SUMMARY = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)")


def _parse_pytest(stdout: str, stderr: str, rc: int) -> dict:
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for n, kind in _SUMMARY.findall(stdout + "\n" + stderr):
        key = "errors" if kind.startswith("error") else kind
        if key in counts:
            counts[key] += int(n)
    # pytest exit codes: 0 ok, 1 tests failed, 2 interrupted, 3 internal,
    # 4 usage, 5 no tests collected.
    return {
        **counts,
        "returncode": rc,
        "ok": rc == 0,
        "no_tests": rc == 5,
    }


class TestRun(Tool):
    name = "test.run"
    description = (
        "Run tests for code in an isolated workdir using a deps-equipped venv. "
        "Provide your test file(s) via `test` (single file) or `files` (map of "
        "path->contents); they run with pytest by default. Tests can import the "
        "orchestrator's own modules (web.server, runtime.*, tools.*) and drive a "
        "FastAPI app in-process with httpx ASGITransport, mocking the model — no "
        "network or live server needed. Quick mode returns parsed pass/fail "
        "inline; set detached=true to run a long suite as a background job "
        "(poll with job.status / job.logs)."
    )
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "test": {
                "type": "string",
                "description": "Contents of a single test file (written as "
                               "test_main.py). Use this OR `files`.",
            },
            "files": {
                "type": "object",
                "description": "Map of relative path -> file contents to write into "
                               "the workdir (test modules named test_*.py, plus any "
                               "fixtures/conftest.py). Use this OR `test`.",
                "additionalProperties": {"type": "string"},
            },
            "command": {
                "type": "string",
                "description": "Override the run command (bash -lc), executed in the "
                               "workdir. Default: pytest -q. Use for -k/-x/markers, "
                               "or to run a plain script instead of pytest.",
            },
            "pythonpath": {
                "type": "string",
                "description": "Extra colon-separated PYTHONPATH entries. The project "
                               "root is always included automatically.",
            },
            "timeout_s": {
                "type": "integer", "default": 120, "minimum": 1, "maximum": 600,
                "description": "Quick-mode wall-clock timeout. Ignored when detached.",
            },
            "detached": {
                "type": "boolean", "default": False,
                "description": "Run as a background job for long suites; returns a "
                               "job_id instead of waiting.",
            },
            "name": {
                "type": "string",
                "description": "Label for the detached job.",
            },
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        cfg = _cfg(ctx)
        files = _collect_files(args)
        if not files:
            return ToolResult(status="error", result=None,
                              error="provide `test` or `files` with at least one file")
        bad = _validate_paths(files)
        if bad:
            return ToolResult(status="error", result=None, error=bad)

        python = cfg.get("python", "python")
        venv_bin = str(Path(python).parent)
        pp = _pythonpath(ctx, args.get("pythonpath"))
        default_cmd = f"{shlex.quote(python)} -m pytest -q"
        command = args.get("command") or default_cmd

        if args.get("detached"):
            return await self._detached(args, ctx, files, command, pp, venv_bin)
        return await self._quick(args, ctx, cfg, files, command, pp, venv_bin)

    # --- quick: bounded subprocess, parsed inline ---
    async def _quick(self, args, ctx, cfg, files, command, pp, venv_bin) -> ToolResult:
        timeout = min(int(args.get("timeout_s", cfg.get("quick_timeout_s", 120))), 600)
        root = Path(cfg.get("workdir_root", "/srv/orchestrator/data/test-runs"))
        root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="quick-", dir=root))
        _write_files(workdir, files)

        import os
        env = os.environ.copy()
        env["PYTHONPATH"] = pp + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PATH"] = venv_bin + ":" + env.get("PATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        prefix = list(cfg.get("sandbox_prefix") or [])
        argv = prefix + ["bash", "-lc", command]
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(workdir), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                import os as _os
                import signal
                try:
                    _os.killpg(_os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
                return ToolResult(status="error", result={"timeout_s": timeout},
                                  error=f"test run timed out after {timeout}s")
            out = out_b.decode("utf-8", "replace")
            err = err_b.decode("utf-8", "replace")
            parsed = _parse_pytest(out, err, proc.returncode)
            parsed["duration_s"] = round(time.monotonic() - t0, 2)
            parsed["stdout"] = out[-6000:]
            parsed["stderr"] = err[-3000:]
            if "No module named pytest" in err:
                return ToolResult(status="error", result=parsed,
                                  error="pytest not found in the configured venv — "
                                        "install it (see requirements-test.txt) or set "
                                        "tools.test.python to a venv that has it")
            status = "ok" if parsed["ok"] else "error"
            err_msg = None if parsed["ok"] else (
                "no tests collected" if parsed["no_tests"]
                else f"{parsed['failed']} failed, {parsed['errors']} errors")
            return ToolResult(status=status, result=parsed, error=err_msg)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # --- detached: hand off to the job runner (full suites) ---
    async def _detached(self, args, ctx, files, command, pp, venv_bin) -> ToolResult:
        cfg = _cfg(ctx)
        root = Path(cfg.get("workdir_root", "/srv/orchestrator/data/test-runs"))
        label = args.get("name", "suite")
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-")[:40] or "suite"
        workdir = root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}"
        workdir.mkdir(parents=True, exist_ok=True)
        _write_files(workdir, files)
        # Delegate to job.start: same detached fire-and-poll machinery, so
        # job.status / job.logs / job.list all see this test run for free.
        job_args = {
            "command": f"export PATH={shlex.quote(venv_bin)}:$PATH\n{command}",
            "name": f"test-{slug}",
            "cwd": str(workdir),
            "env": {"PYTHONPATH": pp, "PYTHONDONTWRITEBYTECODE": "1"},
            "source_env": False,
        }
        res = await JobStart().execute(job_args, ctx)
        if res.status == "ok" and isinstance(res.result, dict):
            res.result["mode"] = "detached"
            res.result["workdir"] = str(workdir)
            res.result["hint"] = ("poll with job.status / job.logs; a 0 exit code "
                                  "means the suite passed")
        return res
