"""code.run — run a shell command synchronously and return its result inline.

This fills the gap between `code.execute` (sandboxed Python *snippet*, no network,
no fs writes — great for math/JSON, useless for real builds) and `job.start`
(detached, GPU, confirmation-gated, polled via job.status/job.logs — too heavy for
the tight inner loop). `code.run` is what a developer reaches for constantly:
`pytest tests/x.py::test_y`, `python -m mypkg`, `make`, `npm test`, `cargo build`,
`tsc` — run it now, get the exit code and a bounded tail of output back in one turn.

Safety posture (mirrors code.execute, not job.start):
- The working directory is confined to `tools.code.run.allowed_roots`
  (falls back to `tools.fs.allowed_roots`). A cwd outside them is refused.
- Runs under a sandbox prefix (firejail by default) when available, with the
  network OFF unless `network: true` is passed AND config permits it. If the
  sandbox binary is missing the command still runs, but the result says so.
- Output is captured and TAIL-bounded so a chatty build never blows context.
- The inherited environment is scrubbed of secrets before spawning (see
  `_scrub_env`): the orchestrator process holds API keys (LITELLM_MASTER_KEY,
  TAVILY_API_KEY, ...) that a model-influenced command could otherwise read
  and exfiltrate. Config `default_env` and caller `env` are applied AFTER the
  scrub, so an explicit operator/caller choice still passes through.
Because it's confined + sandboxed-by-default it is NOT confirmation-gated, so the
agent can lint/build/test in a fast loop. For unsandboxed, networked, GPU, or
long-running work, use `job.start` (which IS confirmation-gated) instead.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult, work_roots


def _cfg(ctx: ToolContext) -> dict:
    return (ctx.config.get("tools", {}).get("code", {}) or {}).get("run", {}) or {}


def _allowed_roots(ctx: ToolContext) -> list[Path]:
    cfg = _cfg(ctx)
    roots = cfg.get("allowed_roots")
    if roots:                              # explicit override, if an operator set one
        return [Path(r).expanduser().resolve() for r in roots]
    return work_roots(ctx)                 # else the run's work_root (+ tmp), like fs.*


def _resolve_cwd(ctx: ToolContext, cwd: str | None) -> Path:
    roots = _allowed_roots(ctx)
    if not roots:
        raise PermissionError("no workspace configured for code.run")
    raw = Path(cwd or _cfg(ctx).get("default_cwd") or str(roots[0])).expanduser()
    p = (raw if raw.is_absolute() else roots[0] / raw).resolve()   # relative -> workspace
    if not any(p == r or r in p.parents for r in roots):
        allowed = ", ".join(str(r) for r in roots)
        raise PermissionError(
            f"cwd {p} is outside your workspace ({allowed}). "
            f"Run inside the project/chat work dir; for anything else use job.start.")
    if not p.exists():
        raise FileNotFoundError(f"cwd does not exist: {p}")
    return p


def _tail(text: str, max_lines: int, max_chars: int) -> tuple[str, bool]:
    truncated = False
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[-max_chars:]
        truncated = True
    return out, truncated


# Env-scrub rule (bug: model-influenced shell commands must not inherit the
# orchestrator's own secrets). Drop a small denylist of known secret names plus
# ANY var whose name ends in _KEY/_TOKEN/_SECRET/_PASSWORD; keep PATH, HOME,
# LANG and ordinary tooling vars. Deliberately simple and conservative.
_SECRET_ENV_NAMES = {
    "LITELLM_MASTER_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TAVILY_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
}
_SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def _scrub_env(env: dict) -> dict:
    """Return a copy of `env` with secrets stripped (rule above)."""
    return {k: v for k, v in env.items()
            if k not in _SECRET_ENV_NAMES
            and not k.upper().endswith(_SECRET_ENV_SUFFIXES)}


class CodeRun(Tool):
    name = "code.run"
    description = (
        "Run a shell command synchronously in a project directory and return its "
        "exit code plus a bounded tail of stdout/stderr — now, in this turn. Use "
        "for the inner dev loop: running tests (pytest path::test), build/format/"
        "type-check commands (make, npm test, cargo build, ruff, mypy, tsc), or any "
        "quick CLI step. Confined to allowed roots and sandboxed (no network) by "
        "default. For long/detached/GPU jobs use job.start instead."
    )
    private = True

    def needs_confirmation(self, args: dict, ctx: ToolContext) -> bool:
        # Safety comes from the sandbox + root confinement, so the fast loop runs
        # ungated. But if the operator disabled the sandbox (sandbox_prefix: []),
        # commands get full host access — gate those behind human approval.
        cfg = _cfg(ctx)
        prefix = cfg.get("sandbox_prefix")
        sandbox_disabled = prefix is not None and len(prefix) == 0
        return sandbox_disabled
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to run (via bash -c). May be multi-line.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory (must be under allowed roots). "
                               "Defaults to tools.code.run.default_cwd.",
            },
            "timeout_s": {
                "type": "integer", "default": 120, "minimum": 1, "maximum": 600,
                "description": "Hard wall-clock timeout; the process group is killed past it.",
            },
            "network": {
                "type": "boolean", "default": False,
                "description": "Allow network access. Only honored if the sandbox "
                               "config permits it (tools.code.run.allow_network).",
            },
            "max_output_lines": {
                "type": "integer", "default": 200, "minimum": 10, "maximum": 2000,
                "description": "Keep only the last N lines of each stream.",
            },
            "env": {
                "type": "object", "additionalProperties": {"type": "string"},
                "description": "Extra environment variables for this command.",
            },
        },
        "required": ["command"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        cfg = _cfg(ctx)
        try:
            cwd = _resolve_cwd(ctx, args.get("cwd"))
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))

        command = args["command"]
        timeout = min(int(args.get("timeout_s", cfg.get("timeout_s", 120))), 600)
        max_lines = int(args.get("max_output_lines", 200))
        max_chars = int(cfg.get("max_output_chars", 12000))

        # Build the sandbox prefix. Default to a conservative firejail wrapper;
        # operators can override or disable via config (empty list = no sandbox).
        want_network = bool(args.get("network", False)) and bool(cfg.get("allow_network", False))
        prefix = cfg.get("sandbox_prefix")
        if prefix is None:
            prefix = ["firejail", "--quiet", "--private-tmp",
                      f"--whitelist={cwd}", "--read-only=/etc"]
            if not want_network:
                prefix = prefix + ["--net=none"]
        sandbox_active = bool(prefix)
        sandbox_note = None
        if sandbox_active:
            import shutil
            binary = prefix[0]
            if not shutil.which(binary):
                sandbox_note = (f"sandbox '{binary}' not found on PATH; ran WITHOUT "
                                f"a sandbox. Install it (e.g. pacman -S {binary}) or set "
                                f"tools.code.run.sandbox_prefix to [] to silence this.")
                prefix = []
                sandbox_active = False

        # Scrub FIRST, then layer config/caller vars on top of the clean base.
        env = _scrub_env(os.environ.copy())
        env.update({k: str(v) for k, v in (cfg.get("default_env") or {}).items()})
        env.update({k: str(v) for k, v in (args.get("env") or {}).items()})

        argv = list(prefix) + ["bash", "-c", command]

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,   # own process group so we can kill the tree
            )
        except FileNotFoundError as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"failed to launch command: {e}")

        timed_out = False
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), 15)
                await asyncio.sleep(0.5)
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                out_b, err_b = b"", b""

        stdout, t1 = _tail(out_b.decode("utf-8", "replace"), max_lines, max_chars)
        stderr, t2 = _tail(err_b.decode("utf-8", "replace"), max_lines, max_chars)
        rc = proc.returncode

        result = {
            "cwd": str(cwd),
            "exit_code": rc,
            "ok": (rc == 0 and not timed_out),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": t1,
            "stderr_truncated": t2,
            "sandboxed": sandbox_active,
            "network": want_network,
        }
        if timed_out:
            result["timed_out"] = True
            result["note"] = f"killed after {timeout}s"
        if sandbox_note:
            result["sandbox_warning"] = sandbox_note

        # A non-zero exit is a normal, useful signal to the agent (a failing test),
        # NOT a tool error — return it as ok so the model reads stdout/stderr.
        return ToolResult(status="ok", result=result, tool_name=self.name)
