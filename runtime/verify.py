"""Verifier gate for supervised runs.

A run with a `verify` check isn't "done" when the model stops — the check must
pass first. This module holds the check execution (sandboxed like code.run),
the tamper detection on protected test files, and the vacuous-pass guard.

Split out of runtime/loop.py — AgentRuntime composes this via VerifyMixin;
the host class must provide self.config.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

from .proc import run as proc_run
from .tool_base import sandbox_missing, scrub_env

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


async def run_check(command, cwd, timeout, config):
    """Run a check command in the same posture as code.run (firejail, no
    network), confined to the work dir. Returns (exit_code, combined_output).

    Module-level so non-loop callers get byte-identical sandbox/timeout
    semantics: the delegate's specialist-authored CHECK line is model-written
    too and must be confined exactly like an auto_verify command — never run
    bare (fail-closed when the sandbox binary is missing)."""
    cfg = (config.get("tools", {}).get("code", {}) or {}).get("run", {}) or {}
    prefix = cfg.get("sandbox_prefix")
    if prefix is None:
        prefix = ["firejail", "--quiet", "--private-tmp",
                  f"--whitelist={cwd}", "--read-only=/etc", "--net=none"]
    missing = sandbox_missing(prefix)
    if missing:
        # Fail closed: the check command is model-influenced, and there's no
        # confirmation hook on this path — never run it bare just because the
        # sandbox binary isn't installed. (sandbox_prefix: [] is the explicit
        # operator opt-in to bare checks and passes through above.)
        return 126, (f"verifier refused: sandbox '{missing}' not found on PATH, "
                     "and running the check WITHOUT a sandbox is not allowed "
                     f"ungated. Install it (e.g. pacman -S {missing}) or set "
                     "tools.code.run.sandbox_prefix to [] to run checks bare.")
    # Scrub the orchestrator's secrets (same rule as code.run) — the check
    # command is model-influenced and its output goes back to the model.
    env = scrub_env(os.environ.copy())
    env.update({k: str(v) for k, v in (cfg.get("default_env") or {}).items()})
    argv = list(prefix) + ["bash", "-c", command]
    try:
        rc, out, err = await proc_run(argv, cwd=str(cwd), env=env,
                                      timeout=timeout)
    except TimeoutError:
        return 124, f"verifier timed out after {timeout}s"
    except Exception as e:
        return 127, f"verifier could not start: {e}"
    text = (out.decode("utf-8", "replace") + err.decode("utf-8", "replace")).strip()
    return rc, text


async def run_authored_check(command, work_root, config):
    """Run a specialist-authored CHECK command (the delegate no-test-suite
    flow) through the verify sandbox above. Returns the result dict attached
    to the delegation envelope: verified is True only on a real exit 0 — the
    vacuous-pass guard applies, same as the loop's verify gate. There is no
    tamper baseline here (the specialist authored the check itself); the
    guard against a fake green is the sandboxed re-execution plus vacuity."""
    vcfg = (config.get("agent", {}) or {}).get("verify", {}) or {}
    try:
        timeout = int(vcfg.get("timeout_s", 180))
    except (TypeError, ValueError):
        timeout = 180
    cwd = Path(work_root) if work_root else Path(".")
    code, out = await run_check(command, cwd, timeout, config)
    tail = "\n".join((out or "").splitlines()[-40:])[-4000:]
    result = {"command": command, "exit_code": code,
              "verified": code == 0 and not _VACUOUS_VERIFY_RE.search(out or ""),
              "output": tail}
    if code == 0 and not result["verified"]:
        result["note"] = ("the check exited 0 but executed NO tests — a "
                          "vacuous pass is not verification")
    return result


# ---- delegation review (agent.verify_delegate_review) ------------------------
# Judgment of a finished delegation moves OFF the brain (the weakest model in
# the loop) onto the strongest available: a fresh-context reviewer turn sees
# only the task, the report, and the evidence — never the builder's reasoning
# trace, which keeps self-review honest on execution slips (forgotten
# requirements, claims the evidence contradicts). Shared blind spots remain a
# same-model limitation; pinning a verify-tagged preset gives a true second
# opinion. The deterministic `verified` flag (authored check / verify gate)
# is unchanged — this adds judgment, it doesn't replace ground truth.
_REVIEW_MAX_TOKENS = 4000     # verdict JSON is small; room for reasoning judges

_REVIEW_SYSTEM = (
    "You are verifying another agent's completed work. You did not do the "
    "work and see none of its reasoning — judge ONLY the report and the "
    "evidence below. A pass requires: every explicit requirement in the task "
    "is addressed, and the evidence is consistent with the report's claims. "
    "Missing, thin, or contradictory evidence is not a pass. Answer with JSON "
    'only: {"verdict": "pass"|"fail"|"unclear", "issues": ["..."], '
    '"confidence": 0.0-1.0}'
)

_TASK_CAP = 3000
_ANSWER_CAP = 5000


async def _default_review_call(config: dict, alias: str,
                               messages: list[dict]) -> dict:
    """One-shot completion through the LiteLLM proxy (same posture as the
    eval judge's model call: alias resolution, no header when the key is
    unset). Returns {status, content, served_model, error}."""
    import httpx

    from runtime.paths import LITELLM_BASE
    from tools.llm.cloud_models import resolve_model_alias
    base = str((config.get("orchestrator") or {}).get("litellm_base")
               or LITELLM_BASE).rstrip("/")
    key = os.environ.get("LITELLM_MASTER_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    body = {"model": resolve_model_alias(alias, config) or alias,
            "messages": messages, "temperature": 0.0,
            "max_tokens": _REVIEW_MAX_TOKENS,
            "response_format": {"type": "json_object"}}
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(f"{base}/v1/chat/completions",
                                  json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {"status": "error", "content": "", "served_model": "",
                "error": f"{type(e).__name__}: {e}"}
    return {"status": "ok",
            "content": data["choices"][0]["message"].get("content") or "",
            "served_model": str(data.get("model") or alias), "error": None}


def _parse_verdict(text: str) -> dict | None:
    """Tolerant JSON extraction of the review verdict (models sometimes wrap
    in prose/fences)."""
    import json
    text = (text or "").strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (ValueError, TypeError):
            return None
    return None


async def review_delegation(task: str, answer: str, evidence: dict,
                            config: dict, *, aliases: list[str],
                            call=None) -> dict | None:
    """Fresh-context review of a finished delegation. `aliases` are tried in
    order (the caller builds them: verify-tagged live slot → the specialist
    that did the work → the allround slot); the brain is deliberately never a
    candidate — the weakness this removes is the weakest model judging the
    strongest's work. None when no alias answered (review skipped, never
    fatal)."""
    import time as _time
    call = call or _default_review_call
    ev_lines = []
    if evidence.get("verify_command"):
        ev_lines.append(f"- verify command: {evidence['verify_command']} "
                        f"(verified={evidence.get('verified')})")
    ac = evidence.get("authored_check")
    if ac:
        ev_lines.append(
            f"- authored check: `{ac.get('command')}` exit "
            f"{ac.get('exit_code')} verified={ac.get('verified')}\n"
            f"  output tail: {str(ac.get('output') or '')[-1500:]}")
    files = evidence.get("files_changed") or []
    ev_lines.append("- files changed: "
                    + (", ".join(map(str, files[:30])) or "(none reported)"))
    user = (f"TASK GIVEN TO THE AGENT:\n{str(task)[-_TASK_CAP:]}\n\n"
            f"AGENT'S FINAL REPORT:\n{str(answer)[:_ANSWER_CAP]}\n\n"
            "EVIDENCE:\n" + "\n".join(ev_lines))
    messages = [{"role": "system", "content": _REVIEW_SYSTEM},
                {"role": "user", "content": user}]
    last_err = "no alias resolved"
    for alias in dict.fromkeys(a for a in aliases if a):
        t0 = _time.monotonic()
        r = await call(config, alias, messages)
        if r.get("status") != "ok":
            last_err = r.get("error") or "call failed"
            continue
        verdict = _parse_verdict(r.get("content") or "")
        if not verdict or "verdict" not in verdict:
            last_err = "unparseable verdict"
            continue
        return {"model": r.get("served_model") or alias,
                "verdict": str(verdict.get("verdict") or "unclear"),
                "issues": [str(i)[:200]
                           for i in (verdict.get("issues") or [])][:10],
                "confidence": verdict.get("confidence"),
                "latency_ms": int((_time.monotonic() - t0) * 1000)}
    log = logging.getLogger(__name__)
    log.warning("delegation review skipped: %s", last_err)
    return None


class VerifyMixin:
    """The verifier-gate half of AgentRuntime (host must provide self.config)."""

    def _normalize_verify(self, verify):
        """A verify arg — a command string, or {command, protect?, max_checks?,
        timeout_s?}, or {hook, ...} with an async callable — into a full spec,
        or None. Config agent.verify fills defaults.

        The hook form is for callers whose check can't be expressed as a
        sandboxed shell command in the work_root (the eval harness's per-case
        checker scripts, which grade container state): `hook(candidate_answer)
        -> (passed, report)` runs instead of the command, and tamper/baseline
        snapshotting (both command-specific) is skipped."""
        if not verify:
            return None
        if isinstance(verify, str):
            verify = {"command": verify}
        if not isinstance(verify, dict):
            # e.g. verify=True — a truthy value with no command to run. There's nothing
            # to verify against, so treat it as "no verification" rather than crashing
            # on verify.get(). (Callers wanting verification must pass a command.)
            return None
        vcfg = (self.config.get("agent", {}) or {}).get("verify", {}) or {}
        hook = verify.get("hook")
        if callable(hook):
            return {
                "hook": hook,
                "command": str(verify.get("command") or "hook check"),
                "protect": [],
                "max_checks": int(verify.get("max_checks") or vcfg.get("max_checks", 4)),
                "timeout_s": int(verify.get("timeout_s") or vcfg.get("timeout_s", 180)),
            }
        cmd = (verify.get("command") or "").strip()
        if not cmd:
            return None
        # protect is list-shaped; models sometimes send protect: true ("yes,
        # guard the tests") or a bare string. A non-list value must NEVER
        # weaken the tamper guard — fall back to the default set, and a
        # string means exactly that one path.
        def _paths(v):
            if isinstance(v, str):
                return [v]
            if isinstance(v, (list, tuple)):
                return [str(p) for p in v]
            return None
        return {
            "command": cmd,
            "protect": (_paths(verify.get("protect"))
                        or _paths(vcfg.get("protect"))
                        or list(_DEFAULT_VERIFY_PROTECT)),
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
        return await run_check(command, cwd, timeout, ctx.config)

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
        # Pre-existing red: the baseline pre-run (loop, before the agent
        # started) failed with the identical signature — this failure is not
        # the agent's and chasing it would burn its checks. Accept as
        # "not worse", with the caveat stated in the report.
        pre = state.get("pre") or {}
        if pre.get("code") not in (None, 0) and _verify_sig(out) == pre.get("sig"):
            return True, (f"verifier: `{spec['command']}` still fails, but the "
                          "failure is IDENTICAL to the pre-existing baseline "
                          "(it was red before your changes) — accepted as "
                          "'not worse'. State the pre-existing failure in your "
                          "summary; do not try to fix it or touch its tests.")
        return False, f"verifier FAILED (exit {code}) — `{spec['command']}`:\n{tail}"
