"""ops.run — a TRUSTED, gated command path for validating/operating the live system.

Unlike code.run (sandboxed: no network, no project venv) this runs a command
directly on the host with the project venv on PATH and loopback network — for
self-validation only: running the project's own tests, curling local services
(the LiteLLM proxy), checking systemd/GPU state.

Guardrails (defence in depth):
  * `requires_confirmation` — the operator approves every call.
  * program allowlist (config `tools.ops.allow`) — argv[0] must be on it.
  * NO shell — argv is exec'd directly (shlex.split), and any shell metacharacter
    (; | & $ ` < > \\ newline) is rejected, so a command can't chain past the
    allowlisted program.
  * network tools (curl/wget/…) are restricted to loopback URLs.
  * bounded wall-clock timeout.
This can validate the box; it can't do arbitrary host surgery.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from runtime.tool_base import Tool, ToolContext, ToolResult

_METACHARS = set(";|&$`<>\n\\")
_NET_PROGS = {"curl", "wget", "http", "https", "httpie"}
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}
_DEFAULT_ALLOW = ["pytest", "python", "python3", "curl", "systemctl", "rocm-smi",
                  "nvidia-smi", "ss", "journalctl", "cat", "grep", "ls", "uv"]

# Scheme-less targets a net tool would still connect to (curl/wget happily fetch
# `curl 10.0.0.1:8080`): a bare IP literal, host:port, or host/path. A bare word
# with neither port nor path (e.g. `-o out.json`) is NOT treated as a target.
_SCHEMALESS_TARGET = re.compile(
    r"^(?:\[(?P<ip6>[0-9a-fA-F:]+)\](?::\d+)?(?:/\S*)?"
    r"|(?P<ip4>\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?(?:/\S*)?"
    r"|(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*)(?::\d+(?:/\S*)?|/\S+))$")


def _arg_host(a: str) -> str | None:
    """Host of a network-target arg: a full URL, or a scheme-less host[:port] /
    host/path / bare-IP token. None when the arg doesn't look like a target."""
    if re.match(r"https?://", a):
        return urlparse(a).hostname or ""
    m = _SCHEMALESS_TARGET.match(a)
    if m:
        return m.group("ip6") or m.group("ip4") or m.group("host")
    return None


def _cfg(ctx: ToolContext) -> dict:
    return (ctx.config.get("tools", {}) or {}).get("ops", {}) or {}


def _validate(command: str, allow: set[str], loopback_only: bool):
    """Return (argv, None) if allowed, else (None, reason)."""
    if any(c in command for c in _METACHARS):
        return None, ("command contains a shell metacharacter (; | & $ ` < > \\) — ops.run "
                      "runs ONE command with no shell, so chaining/pipes/redirects aren't allowed")
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return None, f"could not parse command: {e}"
    if not argv:
        return None, "empty command"
    prog = os.path.basename(argv[0])
    if prog not in allow:
        return None, (f"'{prog}' is not in the ops.run allowlist "
                      f"({', '.join(sorted(allow))}) — extend tools.ops.allow to permit it")
    if loopback_only and prog in _NET_PROGS:
        for a in argv[1:]:
            host = _arg_host(a)
            if host is not None and host not in _LOOPBACK:
                return None, (f"ops.run only allows loopback URLs; '{a}' targets '{host}'. "
                              "It's for validating LOCAL services, not off-box requests.")
    return argv, None


class OpsRun(Tool):
    name = "ops.run"
    description = (
        "Run a TRUSTED host command to validate or operate the LIVE system — project "
        "venv on PATH + loopback network, NO sandbox. Use it to run the project's own "
        "tests against live services, curl local endpoints (the LiteLLM proxy on :4000), "
        "or check systemd/GPU state — the things code.run's sandbox can't reach. One "
        "command, no shell (no pipes/chaining/redirects); the program must be on the "
        "allowlist and network tools may only hit loopback. Confirmation required. NOT "
        "for arbitrary host changes or untrusted computation — use code.run for that."
    )
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string",
                        "description": "A single command (no pipes/;/&&). e.g. "
                                       "'pytest -q tests/test_verify.py', "
                                       "'curl -s http://127.0.0.1:4000/v1/models', "
                                       "'systemctl --user status llama-brain2'."},
            "timeout_s": {"type": "integer", "default": 120, "minimum": 1, "maximum": 600},
        },
        "required": ["command"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        cfg = _cfg(ctx)
        allow = set(cfg.get("allow", _DEFAULT_ALLOW))
        loopback_only = cfg.get("loopback_only", True)
        command = (args.get("command") or "").strip()
        if not command:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="command is required")
        argv, reason = _validate(command, allow, loopback_only)
        if reason:
            return ToolResult(status="error", result=None, tool_name=self.name, error=reason)

        from runtime.paths import HOME as _ORCH_HOME, VENV_BIN as _VENV_BIN
        root = cfg.get("project_root", str(_ORCH_HOME))
        venv_bin = cfg.get("venv_bin", str(_VENV_BIN))
        env = dict(os.environ)
        env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"          # project venv first
        timeout = min(int(args.get("timeout_s", cfg.get("timeout_s", 120))), 600)

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=root, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"program not found: {argv[0]}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"command timed out after {timeout}s")
        rc = proc.returncode
        cap = 20000
        so = out.decode("utf-8", "replace")[:cap]
        se = err.decode("utf-8", "replace")[:cap]
        return ToolResult(status="ok" if rc == 0 else "error", tool_name=self.name, result={
            "command": " ".join(argv), "returncode": rc,
            "stdout": so, "stderr": se,
            "latency_ms": int((time.monotonic() - start) * 1000)})


class OpsStatus(Tool):
    name = "ops.status"
    description = (
        "Check whether the stack is UP — do this FIRST before validating anything "
        "against live services, so you don't loop against a dead proxy. Reports each "
        "configured systemd --user service (active/inactive/failed) and pings each "
        "local endpoint (LiteLLM proxy, brains). Read-only, no confirmation. Use THIS, "
        "not serve.health — serve.* only tracks servers you started with serve.start, "
        "NOT the systemd-managed litellm/brain units, so serve.health can't see them."
    )
    private = True
    read_only = True
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        scfg = _cfg(ctx).get("status", {}) or {}
        services = scfg.get("services", ["litellm-proxy", "llama-brain1", "llama-brain2"])
        pings = scfg.get("pings", {"litellm": "http://127.0.0.1:4000/v1/models"})

        svc: dict[str, str] = {}
        for s in services:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "systemctl", "--user", "is-active", s,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                svc[s] = out.decode("utf-8", "replace").strip() or "unknown"
            except Exception as e:
                svc[s] = f"error: {type(e).__name__}"

        eps: dict[str, dict] = {}
        async with httpx.AsyncClient(timeout=3) as client:
            for name, url in pings.items():
                try:
                    r = await client.get(url)
                    info: dict = {"url": url, "up": r.status_code < 500, "status": r.status_code}
                    # For llama.cpp /health endpoints, try to identify which model is loaded
                    if url.endswith("/health") and r.status_code < 500:
                        try:
                            base = url.rsplit("/health", 1)[0]
                            mr = await client.get(f"{base}/v1/models")
                            if mr.status_code == 200:
                                data = mr.json().get("data", [])
                                if data:
                                    info["model_id"] = data[0].get("id", "unknown")
                        except Exception:
                            pass
                    eps[name] = info
                except Exception as e:
                    eps[name] = {"url": url, "up": False, "error": type(e).__name__}

        all_up = (all(v == "active" for v in svc.values()) and
                  all(e.get("up") for e in eps.values()))
        return ToolResult(status="ok", tool_name=self.name, result={
            "all_up": all_up, "services": svc, "endpoints": eps})
