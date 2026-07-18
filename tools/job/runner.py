"""Detached job runner — the 'real compute' escape hatch.

This is deliberately NOT code.execute. `code.execute` is the safety sandbox
(firejail, no net, no GPU, seconds). This namespace runs real, long-lived
commands with GPU access in a persistent working directory: training runs,
quantization, dataset builds, evals.

Jobs are *detached* — spawned in a new session so they survive an orchestrator
restart and outlive the per-request wall-clock budget. The agent fires a job,
gets a job_id back immediately, then polls status/logs. The filesystem is the
source of truth (one dir per job), so nothing breaks if the runtime dies.

SAFETY: this runs arbitrary shell with your GPUs and no sandbox. It is marked
private (results never auto-forward to cloud LLMs) and requires_confirmation.
Note: the phase 1-4 loop does not yet *honor* requires_confirmation — see the
note in the message that delivered this file if you want the 6-line loop patch.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from runtime.outputs import is_safe_run_id
from runtime.tool_base import Tool, ToolContext, ToolResult


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _cfg(ctx: ToolContext) -> dict:
    return ctx.config.get("tools", {}).get("job", {})


def _jobs_root(ctx: ToolContext) -> Path:
    from runtime.paths import JOBS_DIR
    root = Path(_cfg(ctx).get("jobs_root", str(JOBS_DIR)))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-")[:40] or "job"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_meta(d: Path) -> dict:
    p = d / "meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # kill(pid, 0) also succeeds for a zombie the orchestrator hasn't reaped.
    # Treat zombies (state 'Z') as not alive so killed/exited jobs don't show
    # as 'running'. /proc is Linux-only; fall back to the kill() result.
    try:
        with open(f"/proc/{pid}/stat") as f:
            # ...) <state> ... — state is the field right after the comm in parens
            state = f.read().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (FileNotFoundError, IndexError, ProcessLookupError):
        return False
    except OSError:
        return True


def _pid_cmdline(pid: int) -> str:
    """The process's command line as one string ('' if unreadable). Linux /proc
    only. Used to verify a recorded pid still belongs to THIS job before any
    signal is sent — pids get recycled, and meta.json outlives the process."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _status_of(d: Path) -> dict:
    """Derive a job's current state purely from on-disk artifacts."""
    meta = _read_meta(d)
    ec_file = d / "exit_code"
    state, exit_code = "unknown", None

    if ec_file.exists():
        txt = ec_file.read_text().strip()
        exit_code = int(txt) if txt.lstrip("-").isdigit() else None
        state = "succeeded" if exit_code == 0 else "failed"
    elif meta.get("pid") and _pid_alive(int(meta["pid"])):
        state = "running"
    elif meta.get("pid"):
        # pid gone but exit code never written => killed or hard-crashed
        state = "ended"

    started = meta.get("started_at_epoch")
    if ec_file.exists():
        end = ec_file.stat().st_mtime
    elif state == "running":
        end = time.time()
    else:
        end = (d / "stdout.log").stat().st_mtime if (d / "stdout.log").exists() else None
    runtime_s = round(end - started, 1) if (started and end) else None

    return {
        "job_id": meta.get("job_id", d.name),
        "name": meta.get("name"),
        "state": state,
        "exit_code": exit_code,
        "pid": meta.get("pid"),
        "gpus": meta.get("gpus"),
        "cwd": meta.get("cwd"),
        "command": meta.get("command"),
        "started_at": meta.get("started_at"),
        "runtime_s": runtime_s,
    }


def _tail(path: Path, n: int) -> str:
    if not path.exists():
        return ""
    # Bounded tail without slurping a multi-GB log.
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block = 8192
        data = b""
        while size > 0 and data.count(b"\n") <= n:
            step = min(block, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
    lines = data.decode("utf-8", "replace").splitlines()
    return "\n".join(lines[-n:])


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

class JobStart(Tool):
    name = "job.start"
    description = (
        "Launch a long-running command with GPU access in a persistent working "
        "directory, DETACHED. Returns a job_id immediately; the job keeps running "
        "in the background (survives orchestrator restarts). Use for training, "
        "quantization, dataset builds, evals — anything heavier than code.execute. "
        "Poll progress with job.status and job.logs. NOT sandboxed."
    )
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to run (bash -c). May be multi-line.",
            },
            "name": {
                "type": "string",
                "description": "Short human label for the job (used in the job_id).",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory. Defaults to tools.job.default_cwd.",
            },
            "gpus": {
                "type": "string",
                "description": "HIP_VISIBLE_DEVICES value, e.g. '0' or '0,1'. "
                               "Omit to use the configured default.",
            },
            "env": {
                "type": "object",
                "description": "Extra environment variables for this job.",
                "additionalProperties": {"type": "string"},
            },
            "source_env": {
                "type": "boolean",
                "default": True,
                "description": "Source the configured RDNA4 env script before the command.",
            },
        },
        "required": ["command"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        cfg = _cfg(ctx)
        command = args["command"]
        name = _slug(args.get("name", "job"))
        from runtime.paths import WORK_DIR
        cwd = args.get("cwd") or cfg.get("default_cwd", str(WORK_DIR))
        Path(cwd).mkdir(parents=True, exist_ok=True)

        job_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{name}"
        jdir = _jobs_root(ctx) / job_id
        jdir.mkdir(parents=True, exist_ok=True)

        # Build environment: process env -> config defaults -> gpus -> caller env
        env = os.environ.copy()
        env.update({k: str(v) for k, v in (cfg.get("default_env") or {}).items()})
        env.setdefault("GPU_MAX_HW_QUEUES", "1")          # RDNA4 safe default
        gpus = args.get("gpus", cfg.get("default_gpus"))
        if gpus is not None:
            env["HIP_VISIBLE_DEVICES"] = str(gpus)
        env.update({k: str(v) for k, v in (args.get("env") or {}).items()})

        # Optional: source the user's rdna4-env.sh so its exact workarounds apply.
        env_setup = cfg.get("env_setup")
        source_line = ""
        if args.get("source_env", True) and env_setup and Path(env_setup).exists():
            source_line = f"source {shlex.quote(env_setup)}\n"

        exit_path = jdir / "exit_code"
        run_sh = jdir / "run.sh"
        run_sh.write_text(
            "#!/usr/bin/env bash\n"
            "set -o pipefail\n"
            f"{source_line}"
            f"{command}\n"
            "__rc=$?\n"
            f"echo \"$__rc\" > {shlex.quote(str(exit_path))}\n"
            "exit \"$__rc\"\n"
        )
        run_sh.chmod(0o755)

        stdout_log = jdir / "stdout.log"
        stderr_log = jdir / "stderr.log"
        with stdout_log.open("wb") as out, stderr_log.open("wb") as err:
            proc = subprocess.Popen(
                ["bash", str(run_sh)],
                cwd=cwd,
                env=env,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=True,   # detach: own process group, survives parent
            )

        meta = {
            "job_id": job_id,
            "name": args.get("name", name),
            "command": command,
            "cwd": cwd,
            "gpus": env.get("HIP_VISIBLE_DEVICES"),
            "pid": proc.pid,
            "started_at": _now_iso(),
            "started_at_epoch": time.time(),
            "request_id": ctx.request_id,
        }
        (jdir / "meta.json").write_text(json.dumps(meta, indent=2))

        return ToolResult(status="ok", result={
            "job_id": job_id,
            "pid": proc.pid,
            "gpus": meta["gpus"],
            "log_dir": str(jdir),
            "hint": "wait for it with job.wait; or poll job.status / job.logs",
        })


class JobStatus(Tool):
    name = "job.status"
    poll_safe = True
    description = ("Report a job's state (running/succeeded/failed/ended), exit code, "
                   "pid and runtime. Omit job_id to get the most recent job.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job id from job.start. "
                                                        "Omit for the latest job."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        root = _jobs_root(ctx)
        job_id = args.get("job_id")
        if job_id:
            if not is_safe_run_id(job_id):
                return ToolResult(status="error", result=None,
                                  error=f"invalid job_id: {job_id!r}")
            d = root / job_id
            if not d.exists():
                return ToolResult(status="error", result=None, error=f"no such job: {job_id}")
        else:
            dirs = sorted([p for p in root.iterdir() if p.is_dir()])
            if not dirs:
                return ToolResult(status="ok", result={"jobs": [], "note": "no jobs yet"})
            d = dirs[-1]
        return ToolResult(status="ok", result=_status_of(d))


class JobWait(Tool):
    name = "job.wait"
    poll_safe = True
    description = (
        "Block until a job finishes (or the timeout elapses), then return its final "
        "state, exit code and a tail of its logs. Use this to wait for a job instead "
        "of polling job.status in a loop. If it returns state 'running', the job is "
        "still going — call job.wait again to keep waiting.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job id from job.start. "
                                                        "Omit for the latest job."},
            "timeout_s": {"type": "integer", "default": 120, "minimum": 1, "maximum": 600,
                          "description": "Max seconds to block before returning (default 120)."},
            "tail": {"type": "integer", "default": 40, "minimum": 0, "maximum": 1000,
                     "description": "Trailing log lines per stream to include on finish."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        root = _jobs_root(ctx)
        job_id = args.get("job_id")
        if job_id:
            if not is_safe_run_id(job_id):
                return ToolResult(status="error", result=None,
                                  error=f"invalid job_id: {job_id!r}")
            d = root / job_id
            if not d.exists():
                return ToolResult(status="error", result=None, error=f"no such job: {job_id}")
        else:
            dirs = sorted([p for p in root.iterdir() if p.is_dir()])
            if not dirs:
                return ToolResult(status="error", result=None, error="no jobs yet")
            d = dirs[-1]

        timeout = min(int(args.get("timeout_s", 120)), 600)
        deadline = time.monotonic() + timeout
        interval = 0.5
        st = _status_of(d)
        while st["state"] == "running" and time.monotonic() < deadline:
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(interval * 1.5, 5.0)   # gentle backoff, capped
            st = _status_of(d)

        n = int(args.get("tail", 40))
        if n:
            st = dict(st)
            st["stdout"] = _tail(d / "stdout.log", n)
            st["stderr"] = _tail(d / "stderr.log", n)
        if st["state"] == "running":
            st = dict(st)
            st["note"] = "still running after timeout — call job.wait again to keep waiting"
        return ToolResult(status="ok", result=st)


class JobLogs(Tool):
    name = "job.logs"
    poll_safe = True
    description = ("Return the tail of a job's stdout and stderr. Use to watch "
                   "progress or diagnose a failure.")
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job id. Omit for the latest job."},
            "tail": {"type": "integer", "default": 80, "minimum": 1, "maximum": 1000,
                     "description": "Number of trailing lines per stream."},
            "stream": {"type": "string", "enum": ["stdout", "stderr", "both"],
                       "default": "both"},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        root = _jobs_root(ctx)
        job_id = args.get("job_id")
        if job_id:
            if not is_safe_run_id(job_id):
                return ToolResult(status="error", result=None,
                                  error=f"invalid job_id: {job_id!r}")
            d = root / job_id
            if not d.exists():
                return ToolResult(status="error", result=None, error=f"no such job: {job_id}")
        else:
            dirs = sorted([p for p in root.iterdir() if p.is_dir()])
            if not dirs:
                return ToolResult(status="error", result=None, error="no jobs yet")
            d = dirs[-1]

        n = min(int(args.get("tail", 80)), 1000)
        stream = args.get("stream", "both")
        out = {"job_id": d.name, "state": _status_of(d)["state"]}
        if stream in ("stdout", "both"):
            out["stdout"] = _tail(d / "stdout.log", n)
        if stream in ("stderr", "both"):
            out["stderr"] = _tail(d / "stderr.log", n)
        return ToolResult(status="ok", result=out)


class JobList(Tool):
    name = "job.list"
    poll_safe = True
    description = "List recent jobs with their state, newest first."
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            "state": {"type": "string", "enum": ["running", "succeeded", "failed",
                                                 "ended", "unknown", "any"],
                      "default": "any"},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        root = _jobs_root(ctx)
        dirs = sorted([p for p in root.iterdir() if p.is_dir()], reverse=True)
        want = args.get("state", "any")
        limit = int(args.get("limit", 20))
        rows = []
        for d in dirs:
            st = _status_of(d)
            if want != "any" and st["state"] != want:
                continue
            rows.append({k: st[k] for k in
                         ("job_id", "name", "state", "exit_code", "runtime_s", "gpus")})
            if len(rows) >= limit:
                break
        return ToolResult(status="ok", result={"jobs": rows, "count": len(rows)})


class JobCancel(Tool):
    name = "job.cancel"
    description = ("Terminate a running job (SIGTERM, then SIGKILL after a grace "
                   "period). No-op if the job already finished.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job id to cancel."},
            "grace_s": {"type": "integer", "default": 5, "minimum": 0, "maximum": 60},
        },
        "required": ["job_id"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        job_id = args["job_id"]
        # job_id is used verbatim as a path component under the jobs root (and
        # its meta.json names the pid to signal) — reject anything that isn't
        # a single plain name before it can traverse out or point at a victim.
        if not is_safe_run_id(job_id):
            return ToolResult(status="error", result=None,
                              error=f"invalid job_id: {job_id!r}")
        d = _jobs_root(ctx) / job_id
        if not d.exists():
            return ToolResult(status="error", result=None, error=f"no such job: {job_id}")
        # A written exit code means the job already finished — report the final
        # state, never signal anything.
        if (d / "exit_code").exists():
            st = _status_of(d)
            return ToolResult(status="ok", result={
                "job_id": d.name, "state": st["state"], "exit_code": st["exit_code"],
                "note": "already finished"})
        meta = _read_meta(d)
        pid = meta.get("pid")
        if not pid or not _pid_alive(int(pid)):
            return ToolResult(status="ok", result={"job_id": d.name, "note": "not running"})

        pid = int(pid)
        # Pids get recycled: never signal a group without proving the pid is
        # still OUR job. job.start launches `bash <job_dir>/run.sh`, so the job
        # dir name must appear in /proc/<pid>/cmdline (the recorded `command`
        # only shows up in the wrapper's *children*, not the wrapper itself).
        def identity_ok() -> bool:
            cmdline = _pid_cmdline(pid)
            return bool(cmdline) and d.name in cmdline

        if not identity_ok():
            return ToolResult(status="error", result=None, error=(
                f"refusing to cancel: pid {pid} is alive but its command line does "
                f"not reference job '{d.name}' — the pid was likely recycled by an "
                f"unrelated process. Inspect and clean up the stale job dir manually."))

        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return ToolResult(status="ok", result={"job_id": d.name, "note": "already gone"})

        os.killpg(pgid, signal.SIGTERM)
        grace = int(args.get("grace_s", 5))
        deadline = time.time() + grace
        while time.time() < deadline and _pid_alive(pid):
            await asyncio.sleep(0.2)   # never block the event loop during grace
        killed = "SIGTERM"
        rc = 143  # 128 + SIGTERM
        if _pid_alive(pid):
            if not identity_ok():
                # The pid changed hands during the grace window — do NOT escalate.
                return ToolResult(status="error", result=None, error=(
                    f"pid {pid} no longer matches job '{d.name}' after SIGTERM; "
                    f"not escalating to SIGKILL (likely pid reuse)."))
            os.killpg(pgid, signal.SIGKILL)
            killed = "SIGKILL"
            rc = 137  # 128 + SIGKILL
        # The kill prevents the wrapper from recording its own exit code, so
        # write one ourselves — keeps job.status deterministic (-> 'failed').
        ec_file = d / "exit_code"
        if not ec_file.exists():
            ec_file.write_text(str(rc))
        return ToolResult(status="ok", result={"job_id": d.name, "signal": killed,
                                               "exit_code": rc})
