"""Verifier gate for supervised runs.

A run with a `verify` check isn't "done" when the model stops — the check must
pass first. This module holds the check execution (sandboxed like code.run),
the tamper detection on protected test files, and the vacuous-pass guard.

Split out of runtime/loop.py — AgentRuntime composes this via VerifyMixin;
the host class must provide self.config.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from pathlib import Path

from .tool_base import scrub_env

# Default set of files a verifier owns and the agent must NOT edit to "pass":
# test modules + pytest conftest. Snapshotted before the run; a change = tampering.
_DEFAULT_VERIFY_PROTECT = ["**/test_*.py", "**/*_test.py", "**/tests/**/*.py", "**/conftest.py"]
# A "green" check that actually executed nothing — the classic way to fake a pass.
_VACUOUS_VERIFY_RE = re.compile(r"no tests ran|collected 0 items|=+ *0 passed", re.I)


def _verify_sig(report: str) -> str:
    """A stable fingerprint of a verifier failure, ignoring run-to-run noise
    (durations, counts, tmp paths, addresses). Same fingerprint twice => the
    agent is stuck on the identical failure, i.e. making no progress."""
    s = re.sub(r"/tmp/\S+|0x[0-9a-fA-F]+", "", report or "")
    s = re.sub(r"\d+", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:16]


class VerifyMixin:
    """The verifier-gate half of AgentRuntime (host must provide self.config)."""

    def _normalize_verify(self, verify):
        """A verify arg — a command string, or {command, protect?, max_checks?,
        timeout_s?} — into a full spec, or None. Config agent.verify fills defaults."""
        if not verify:
            return None
        if isinstance(verify, str):
            verify = {"command": verify}
        if not isinstance(verify, dict):
            # e.g. verify=True — a truthy value with no command to run. There's nothing
            # to verify against, so treat it as "no verification" rather than crashing
            # on verify.get(). (Callers wanting verification must pass a command.)
            return None
        cmd = (verify.get("command") or "").strip()
        if not cmd:
            return None
        vcfg = (self.config.get("agent", {}) or {}).get("verify", {}) or {}
        return {
            "command": cmd,
            "protect": list(verify.get("protect") or vcfg.get("protect")
                            or _DEFAULT_VERIFY_PROTECT),
            "max_checks": int(verify.get("max_checks") or vcfg.get("max_checks", 4)),
            "timeout_s": int(verify.get("timeout_s") or vcfg.get("timeout_s", 180)),
        }

    @staticmethod
    def _snapshot_protected(work_root, patterns):
        """sha256 of every file matching the protect globs — the verifier's own
        code (tests/conftest) the agent must not rewrite to force a pass."""
        snap: dict[str, str] = {}
        if not work_root:
            return snap
        root = Path(work_root)
        for pat in patterns:
            try:
                for p in root.glob(pat):
                    if p.is_file():
                        snap[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
            except Exception:
                continue
        return snap

    async def _run_verify_command(self, command, cwd, timeout, ctx):
        """Run the check in the same posture as code.run (firejail, no network),
        confined to the work dir. Returns (exit_code, combined_output)."""
        cfg = (ctx.config.get("tools", {}).get("code", {}) or {}).get("run", {}) or {}
        prefix = cfg.get("sandbox_prefix")
        if prefix is None:
            prefix = ["firejail", "--quiet", "--private-tmp",
                      f"--whitelist={cwd}", "--read-only=/etc", "--net=none"]
        if prefix and not shutil.which(prefix[0]):
            prefix = []                       # sandbox binary missing → run bare
        # Scrub the orchestrator's secrets (same rule as code.run) — the check
        # command is model-influenced and its output goes back to the model.
        env = scrub_env(os.environ.copy())
        env.update({k: str(v) for k, v in (cfg.get("default_env") or {}).items()})
        argv = list(prefix) + ["bash", "-c", command]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
        except Exception as e:
            return 127, f"verifier could not start: {e}"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return 124, f"verifier timed out after {timeout}s"
        text = (out.decode("utf-8", "replace") + err.decode("utf-8", "replace")).strip()
        return proc.returncode, text

    async def _verify(self, spec, state, ctx, work_root):
        """Run the verifier once. Returns (passed, report). Fails on non-zero exit,
        a change to any protected test/check file (tampering), or a vacuous pass
        (exit 0 but zero tests executed)."""
        cwd = Path(work_root) if work_root else Path(".")
        code, out = await self._run_verify_command(spec["command"], cwd, spec["timeout_s"], ctx)
        tail = "\n".join((out or "").splitlines()[-40:])[-4000:]
        now = self._snapshot_protected(work_root, spec["protect"])
        base = state.get("baseline") or {}
        # Tampering = a baseline file MODIFIED or DELETED. A file newly CREATED
        # under the protect globs is not tampering — the delegate flow has the
        # agent write its own tests first, then implement against them.
        tampered = sorted(k for k in base if k not in now or now[k] != base[k])
        if tampered:
            return False, ("VERIFIER TAMPERING — the protected test/check files changed: "
                           f"{', '.join(tampered[:10])}. Revert them; make the real code "
                           "satisfy the existing tests, do not edit the tests.")
        if code == 0 and _VACUOUS_VERIFY_RE.search(out or ""):
            return False, ("The check exited 0 but executed NO tests — that is not a pass. "
                           f"Make the tests actually run.\n\n{tail}")
        if code == 0:
            return True, f"verifier passed: `{spec['command']}`"
        return False, f"verifier FAILED (exit {code}) — `{spec['command']}`:\n{tail}"
