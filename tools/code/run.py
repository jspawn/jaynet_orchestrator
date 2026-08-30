"""code.run — the ONE code execution tool: run code now, get the result now.

Two languages, one verb:
- language=bash (default): a shell command for the inner dev loop —
  `pytest tests/x.py::test_y`, `make`, `npm test`, `cargo build`, `ruff`.
- language=python: a short Python snippet for math, JSON/regex parsing, quick
  computation, small plots. numpy/matplotlib available (Agg backend forced);
  files written to the ORCH_EXEC_OUT dir come back as written_files (that's
  how charts get out); relative file ops land in the persistent ORCH_EXEC_WORK
  workspace that survives across calls within the run.

The HARNESS picks the backend — the model never has to think about it:
1. Eval case container (tools.code.container, injected by the eval runner via
   run_overrides tools_patch — Terminal-Bench full mode): both languages exec
   inside the case's podman container. Its workdir IS the run's work_root
   (bind-mounted), so files persist across calls and absolute container paths
   (/app/...) are valid. Never gated: the container is the sandbox.
2. Devbox toolchain container (tools.code.devbox.enabled): commands run in
   this run's per-run container — full rust/go/node/C-C++/.NET/java
   environments with cached deps. Falls back to firejail with a note when
   unavailable.
3. Host firejail (default): bash runs under the sandbox prefix (network off
   unless permitted); python runs with the import-scrub preamble, rlimits,
   ORCH_EXEC_OUT/WORK channels and the mediated subcall (llm_query) seam.

Safety posture (mirrors the old code.execute, not job.start):
- The working directory is confined to `tools.code.run.allowed_roots`
  (falls back to the run's work roots). A cwd outside them is refused.
- If the sandbox binary is missing the call is confirmation-gated (see
  needs_confirmation); after approval it runs unsandboxed and the result
  says so. Bare execution is never silent.
- Output is captured and bounded so a chatty build never blows context;
  oversized python output spills to a returned file path.
- The inherited environment is scrubbed of secrets before spawning (see
  `scrub_env` in runtime.tool_base.py). Config `default_env` and caller `env`
  are applied AFTER the scrub.

Result contract: status is "error" only when the TOOL couldn't run (launch
failure, confinement violation, timeout kill). A non-zero exit is a normal
signal (a failing test) — status "ok", payload carries ok/exit_code. For
long/detached/GPU jobs use job.start (confirmation-gated, polled via
job.status/job.logs) instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import textwrap
import uuid
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult, sandbox_missing, scrub_env, work_roots

_log = logging.getLogger(__name__)


def _cfg(ctx: ToolContext) -> dict:
    return (ctx.config.get("tools", {}).get("code", {}) or {}).get("run", {}) or {}


def _code_cfg(ctx: ToolContext) -> dict:
    return ctx.config.get("tools", {}).get("code", {}) or {}


def _container_cfg(cfg: dict) -> dict | None:
    """The per-run container binding ({id, workdir, python}) when the eval
    runner injected one via run_overrides tools_patch, else None. There is
    deliberately no static-config path that turns this on for chats
    (runtime/config_loader strips a static key)."""
    ctr = cfg.get("container")
    if isinstance(ctr, dict) and str(ctr.get("id") or "").strip():
        return ctr
    return None


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


# --- python snippet machinery (absorbed from the old code.execute) -----------

_PREAMBLE = """\
# Auto-injected preamble: bounded imports, no network, no fs writes outside cwd
import sys, os
# Drop personal /home site dirs, but keep the interpreter itself: uv-managed
# pythons ship their stdlib under ~/.local and a venv may live under /home —
# stripping those wholesale left snippets without json (or the venv's deps).
_stdlib = os.path.dirname(os.__file__)
sys.path = [p for p in sys.path if p and (
    not p.startswith('/home')
    or p == _stdlib or p.startswith(_stdlib + os.sep)
    or p == sys.prefix or p.startswith(sys.prefix + os.sep))]
# Persistent per-run workspace (set when the run has a work_root): relative
# file ops land in ORCH_EXEC_WORK and survive across code.run python calls.
os.chdir(os.environ.get('ORCH_EXEC_WORK') or os.getcwd())
"""

# Stream caps returned inline for python mode; past a cap the FULL text is
# written to the artifact dir and the path travels instead.
_OUT_CAP = 5000
_ERR_CAP = 1000
# Filenames the spill mechanism uses — excluded from written_files so a spill
# doesn't land in the deliver-these artifacts list (audit D2).
_SPILL_NAMES = frozenset({"stdout.txt", "stderr.txt"})

# Container mode has no subcall seam (the mediation unix socket can't be
# mounted into a running container) — note it once per process, not per call.
_subcall_note_logged = False


def _artifacts(out_dir: Path | None) -> list[str]:
    if not out_dir or not out_dir.exists():
        return []
    return sorted(str(p) for p in out_dir.iterdir()
                  if p.is_file() and p.name not in _SPILL_NAMES)


def _spill_streams(out: str, err: str, out_dir: Path | None) -> dict:
    files: dict[str, str] = {}
    if out_dir is None:
        return files
    for text, cap, name, key in (
            (out, _OUT_CAP, "stdout.txt", "stdout_file"),
            (err, _ERR_CAP, "stderr.txt", "stderr_file")):
        if len(text) > cap:
            p = out_dir / name
            p.write_text(text, encoding="utf-8")
            files[key] = str(p)
    return files


def _bind_rw(path) -> list[str]:
    """--read-write bind args for one path. A path under /tmp ALSO needs
    --whitelist: --private-tmp mounts a fresh tmpfs OVER /tmp after the
    bind, hiding it (verified: bind alone → ENOENT; +whitelist → works)."""
    p = str(path)
    args = [f"--read-write={p}"]
    if p.startswith("/tmp/"):
        args.append(f"--whitelist={p}")
    return args


class CodeRun(Tool):
    name = "code.run"
    description = (
        "Run code synchronously and get the result in this turn: a shell "
        "command (language=bash, default) for the dev loop — running tests "
        "(pytest path::test), build/format/type-check commands (make, npm "
        "test, cargo build, ruff, mypy) or any quick CLI step — or a Python "
        "snippet (language=python) for math, JSON/regex parsing, quick "
        "computation, small plots. Sandboxed and confined to the run's "
        "workspace; the harness picks the sandbox backend. Python mode: "
        "numpy/matplotlib available (Agg backend); save files to the "
        "ORCH_EXEC_OUT dir (os.environ['ORCH_EXEC_OUT']) — they come back as "
        "written_files, hand them to the user with deliver.files. Relative "
        "file ops land in the persistent ORCH_EXEC_WORK workspace: files "
        "there SURVIVE across calls within the run. Print results; "
        "oversized output spills to a returned file path. When the env var "
        "ORCH_SUBCALL_SOCK is set, llm_query(prompt, ...) and "
        "llm_query_batched(prompts, ...) are pre-defined: mediated sub-LLM "
        "calls billed to this run — map LLM work over SLICES of a large file "
        "instead of reading it whole into your context. For long/detached/"
        "GPU jobs use job.start instead. Never import subprocess/os.system "
        "to dodge the sandbox. A declined call is final — verify with fs.* "
        "reads instead of retrying."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command (language=bash, via bash -c; may "
                               "be multi-line) or Python source (language="
                               "python; use print() to return values).",
            },
            "language": {
                "type": "string", "enum": ["bash", "python"], "default": "bash",
                "description": "bash: shell command. python: sandboxed snippet "
                               "with ORCH_EXEC_OUT/WORK file channels.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory (must be under allowed "
                               "roots; ignored in container benchmark runs). "
                               "Defaults to tools.code.run.default_cwd.",
            },
            "timeout_s": {
                "type": "integer", "default": 120, "minimum": 1, "maximum": 600,
                "description": "Hard wall-clock timeout; the process group is killed past it.",
            },
            "network": {
                "type": "boolean", "default": False,
                "description": "Allow network access (host bash sandbox only). "
                               "Only honored if the sandbox config permits it "
                               "(tools.code.run.allow_network).",
            },
            "max_output_lines": {
                "type": "integer", "default": 200, "minimum": 10, "maximum": 2000,
                "description": "Keep only the last N lines of each stream (bash).",
            },
            "env": {
                "type": "object", "additionalProperties": {"type": "string"},
                "description": "Extra environment variables for this command.",
            },
        },
        "required": ["command"],
    }

    def needs_confirmation(self, args: dict, ctx: ToolContext) -> bool:
        code_cfg = _code_cfg(ctx)
        # Container mode (eval-runner managed, via run_overrides tools_patch
        # only): the container IS the sandbox — never gated.
        if _container_cfg(code_cfg) is not None:
            return False
        # Same for the devbox: the container is the sandbox (same rule as the
        # eval harness's container mode). Execution falls back to the
        # firejail path (and its own gates) when the image is missing.
        from tools.code import devbox
        if devbox.enabled(ctx):
            if shutil.which("podman") is not None:
                return False
        if str(args.get("language") or "bash") == "python":
            # Sandboxed (firejail) by default. If the operator disabled the
            # sandbox (sandbox: null/other) or firejail isn't installed, the
            # snippet would run bare on the host — gate that behind human
            # approval instead of silently degrading.
            sandbox = code_cfg.get("sandbox", "firejail")
            if sandbox != "firejail":
                return True
            return sandbox_missing(["firejail"]) is not None
        cfg = _cfg(ctx)
        prefix = cfg.get("sandbox_prefix")
        if prefix is not None and len(prefix) == 0:
            return True
        return sandbox_missing(prefix or ["firejail"]) is not None

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        code_cfg = _code_cfg(ctx)
        language = str(args.get("language") or "bash")
        command = args["command"]
        container = _container_cfg(code_cfg)

        # Eval case container (Terminal-Bench full mode): both languages run
        # inside the case's podman container; its workdir is the run's
        # work_root, so absolute container paths (/app/...) just work.
        if container is not None:
            timeout = min(int(args.get("timeout_s", 120)), 600)
            result = await self._execute_container(
                command, container, timeout, ctx, language=language,
                extra_env={k: str(v)
                           for k, v in (args.get("env") or {}).items()})
            result.tool_name = self.name
            return result

        cfg = _cfg(ctx)
        try:
            cwd = _resolve_cwd(ctx, args.get("cwd"))
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))

        timeout = min(int(args.get("timeout_s", cfg.get("timeout_s", 120))), 600)
        max_lines = int(args.get("max_output_lines", 200))
        max_chars = int(cfg.get("max_output_chars", 12000))

        # Devbox backend: per-run toolchain container (full rust/go/node/C
        # environments, cached deps). Unavailable → classic path with a note.
        from tools.code import devbox
        sandbox_note = None
        if devbox.enabled(ctx):
            if language == "python":
                result, note = await self._devbox_python(
                    args, ctx, cwd, command, timeout, max_lines, max_chars)
            else:
                result, note = await devbox.attempt(
                    args, ctx, cwd, command, timeout, max_lines, max_chars)
            if result is not None:
                result.tool_name = self.name
                return result
            sandbox_note = note

        if language == "python":
            result = await self._python_host(command, args, ctx, timeout,
                                             sandbox_note)
            result.tool_name = self.name
            return result
        result = await self._bash_host(command, args, ctx, cwd, cfg, timeout,
                                       max_lines, max_chars, sandbox_note)
        result.tool_name = self.name
        return result

    # ---- backend: eval case container (both languages) ----------------------

    @staticmethod
    def _build_container_cmd(ctr_id: str, ctr_workdir: str, interpreter: str,
                             script_rel: str, timeout: int,
                             env: dict) -> list[str]:
        """`podman exec` argv for one snippet run. coreutils `timeout` bounds
        the snippet INSIDE the container (the asyncio wait_for in
        _execute_container is the backstop against a stuck podman itself)."""
        cmd = ["podman", "exec", "--workdir", ctr_workdir]
        for k, v in env.items():
            cmd += ["--env", f"{k}={v}"]
        cmd += [ctr_id, "timeout", str(timeout), interpreter, script_rel]
        return cmd

    async def _execute_container(self, code: str, container: dict,
                                 timeout: int, ctx: ToolContext,
                                 language: str = "bash",
                                 extra_env: dict | None = None) -> ToolResult:
        """Run the command inside the eval case's podman container. The runner
        bind-mounts the run's work_root at the container workdir, so the
        per-call script and every file the command writes are shared state —
        there is no per-call workdir here by design (a real terminal keeps
        its cwd too). The artifact dir is the one exception: per-call, so
        concurrent executes don't clobber each other's written_files."""
        global _subcall_note_logged
        if not getattr(ctx, "work_root", None):
            return ToolResult(status="error", result=None,
                              error="container mode requires a run work_root "
                                    "(the eval runner bind-mounts it)")
        ctr_id = str(container["id"]).strip()
        ctr_workdir = str(container.get("workdir") or "/app").rstrip("/") or "/"
        work_root = Path(ctx.work_root)
        if language == "bash":
            interpreter, suffix, source = "bash", ".sh", textwrap.dedent(code)
        else:
            interpreter = str(container.get("python") or "python3")
            suffix, source = ".py", _PREAMBLE + "\n" + textwrap.dedent(code)

        # The subcall/llm_query seam can't cross into a running container
        # (unix socket) — skip the grant silently; the snippet just runs
        # without the helpers.
        if language == "python" \
                and getattr(ctx, "subcall_grant", None) is not None \
                and not _subcall_note_logged:
            _subcall_note_logged = True
            _log.info("code.run container mode: subcall (llm_query) seam "
                      "unavailable — snippets run without helpers")

        script_name = f".orch-exec-{uuid.uuid4().hex[:12]}{suffix}"
        script = work_root / script_name
        # Artifact dir: same host-side layout as the firejail path (under
        # work_root/exec-out/), but ORCH_EXEC_OUT carries the CONTAINER path —
        # the mount makes both views the same directory.
        out_dir = work_root / "exec-out" / script.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        env_in = {"ORCH_EXEC_OUT": f"{ctr_workdir}/exec-out/{script.stem}",
                  "MPLBACKEND": "Agg",
                  "OMP_NUM_THREADS": "1",
                  "OPENBLAS_NUM_THREADS": "1"}
        env_in.update(extra_env or {})
        try:
            script.write_text(source, encoding="utf-8")
            cmd = self._build_container_cmd(ctr_id, ctr_workdir, interpreter,
                                            script_name, timeout, env_in)
            # Secrets stay out of the podman client process env; the command's
            # env is exactly env_in (passed via --env).
            env = scrub_env(dict(os.environ))
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_root),
                env=env,
                start_new_session=True,
            )
            try:
                # Backstop grace over the in-container `timeout` so a wedged
                # podman exec can't hang the run forever.
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout + 15)
            except TimeoutError:
                if getattr(proc, "pid", None) is not None:
                    try:
                        os.killpg(os.getpgid(proc.pid), 15)
                        await asyncio.sleep(0.5)
                        os.killpg(os.getpgid(proc.pid), 9)
                    except (ProcessLookupError, PermissionError):
                        pass
                else:
                    proc.kill()
                await proc.wait()
                return ToolResult(status="error", result=None,
                                  error=f"execution timeout after {timeout}s")

            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            artifacts = _artifacts(out_dir)
            stream_files = _spill_streams(out, err, out_dir)
            return ToolResult(status="ok", result={
                "stdout": out[-_OUT_CAP:],
                "stderr": err[-_ERR_CAP:] if err else "",
                "exit_code": proc.returncode or 0,
                "ok": (proc.returncode or 0) == 0,
                "out_dir": str(out_dir), "written_files": artifacts,
                "sandbox": "container", "container": ctr_id,
                **stream_files,
            })
        finally:
            script.unlink(missing_ok=True)

    # ---- backend: devbox python (bash path lives in devbox.attempt) ---------

    async def _devbox_python(self, args: dict, ctx: ToolContext, cwd: Path,
                             code: str, timeout: int, max_lines: int,
                             max_chars: int):
        """Python snippet inside the devbox container: wrapped as a heredoc
        `python3 -` bash command (devbox.attempt's transport), with the
        ORCH_EXEC_OUT/WORK channels pointing at the /work mount so artifacts
        land back on the host. The container python is clean, so the host
        import-scrub preamble is skipped; the chdir-to-workspace half stays."""
        from tools.code import devbox
        out_dir = None
        env_add = {"MPLBACKEND": "Agg", "OMP_NUM_THREADS": "1",
                   "OPENBLAS_NUM_THREADS": "1"}
        if getattr(ctx, "work_root", None):
            call = f"py-{uuid.uuid4().hex[:12]}"
            out_dir = Path(ctx.work_root) / "exec-out" / call
            out_dir.mkdir(parents=True, exist_ok=True)
            (Path(ctx.work_root) / "exec-work").mkdir(parents=True,
                                                      exist_ok=True)
            env_add["ORCH_EXEC_OUT"] = f"/work/exec-out/{call}"
            env_add["ORCH_EXEC_WORK"] = "/work/exec-work"
        marker = f"ORCH_PY_{uuid.uuid4().hex[:8]}"
        pre = ("import os\n"
               "os.chdir(os.environ.get('ORCH_EXEC_WORK') or os.getcwd())\n")
        command = (f"python3 - <<'{marker}'\n{pre}"
                   f"{textwrap.dedent(code)}\n{marker}")
        args2 = dict(args)
        args2["env"] = {**env_add,
                        **{k: str(v) for k, v in (args.get("env") or {}).items()}}
        result, note = await devbox.attempt(args2, ctx, cwd, command, timeout,
                                            max_lines, max_chars)
        if result is not None and out_dir is not None \
                and isinstance(result.result, dict):
            result.result["out_dir"] = str(out_dir)
            result.result["written_files"] = _artifacts(out_dir)
        return result, note

    # ---- backend: host firejail, bash ----------------------------------------

    async def _bash_host(self, command: str, args: dict, ctx: ToolContext,
                         cwd: Path, cfg: dict, timeout: int, max_lines: int,
                         max_chars: int, sandbox_note: str | None) -> ToolResult:
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
        missing = sandbox_missing(prefix) if sandbox_active else None
        if missing:
            # Only reachable after human approval — needs_confirmation gates the
            # missing-sandbox case. The note keeps the degradation explicit.
            sandbox_note = (f"sandbox '{missing}' not found on PATH; ran WITHOUT "
                            f"a sandbox. Install it (e.g. pacman -S {missing}) or set "
                            f"tools.code.run.sandbox_prefix to [] to silence this.")
            prefix = []
            sandbox_active = False

        # Scrub FIRST, then layer config/caller vars on top of the clean base.
        env = scrub_env(os.environ.copy())
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
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), 15)
                await asyncio.sleep(0.5)
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=5)
            except (TimeoutError, Exception):
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

    # ---- backend: host firejail, python --------------------------------------

    async def _python_host(self, code: str, args: dict, ctx: ToolContext,
                           timeout: int, sandbox_note: str | None) -> ToolResult:
        cfg = _code_cfg(ctx)
        sandbox = cfg.get("sandbox", "firejail")
        # Per-CALL sandbox dir under the configured base. Deliberately NOT the
        # run's /tmp tmp_root: firejail runs with --private-tmp, which mounts a
        # fresh /tmp and would HIDE any workdir living under /tmp. The base
        # must be a real, non-/tmp path. A unique dir per call means concurrent
        # (parallel) executes don't clobber each other, and it's removed in
        # `finally`, so each call is self-cleaning.
        from runtime.paths import SANDBOX_DIR
        base = Path(cfg.get("workdir", str(SANDBOX_DIR)))
        base.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="exec-", dir=base))

        # Artifact dir: files written here SURVIVE the call (charts, CSVs) and
        # are returned for delivery. exec_work: the run's PERSISTENT workspace
        # — the snippet's cwd (via the preamble chdir), survives across calls.
        out_dir = None
        exec_work = None
        if getattr(ctx, "work_root", None):
            out_dir = Path(ctx.work_root) / "exec-out" / workdir.name
            out_dir.mkdir(parents=True, exist_ok=True)
            exec_work = Path(ctx.work_root) / "exec-work"
            exec_work.mkdir(parents=True, exist_ok=True)

        full_source = _PREAMBLE + "\n" + textwrap.dedent(code)

        # Mediated sub-LLM seam (RLM primitive, runtime/subcall.py): when the
        # run offers grants, mint one per execution and hand the snippet the
        # unix-socket path + token plus the llm_query helpers. A grant failure
        # must not break code execution — the snippet runs without helpers.
        subcall = None
        grant_fn = getattr(ctx, "subcall_grant", None)
        if grant_fn is not None:
            try:
                subcall = await grant_fn({})
            except Exception:
                _log.warning(
                    "subcall grant failed — code.run python runs without llm_query",
                    exc_info=True)
        if subcall:
            from runtime.subcall import CLIENT_PREAMBLE
            full_source = _PREAMBLE + "\n" + CLIENT_PREAMBLE + "\n" \
                + textwrap.dedent(code)

        with tempfile.NamedTemporaryFile(
                "w", suffix=".py", dir=workdir, delete=False) as f:
            f.write(full_source)
            script = f.name

        # Secrets stay out of the sandbox; MPLBACKEND keeps matplotlib headless;
        # single BLAS thread keeps numpy's appetite inside the rlimit.
        env = scrub_env(dict(os.environ))
        env.setdefault("MPLBACKEND", "Agg")
        env.setdefault("OMP_NUM_THREADS", "1")
        env.setdefault("OPENBLAS_NUM_THREADS", "1")
        if out_dir:
            env["ORCH_EXEC_OUT"] = str(out_dir)
        if exec_work:
            env["ORCH_EXEC_WORK"] = str(exec_work)
        if subcall:
            # Injected AFTER scrub_env on purpose: the per-execution token is
            # a bearer for the mediation socket, and scrub_env strips *_TOKEN.
            env["ORCH_SUBCALL_SOCK"] = subcall["sock"]
            env["ORCH_SUBCALL_TOKEN"] = subcall["token"]
        env.update({k: str(v) for k, v in (args.get("env") or {}).items()})

        try:
            cmd = self._build_python_cmd(script, sandbox, workdir, out_dir,
                                         cfg.get("python", "python"),
                                         int(cfg.get("rlimit_as_mb", 1024)),
                                         subcall_sock=subcall["sock"] if subcall else None,
                                         exec_work=exec_work)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                env=env,
                start_new_session=True,   # own process group so we can kill the tree
            )
            timed_out = False
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                timed_out = True
                # Kill the whole process group, not just the direct child —
                # sandbox grandchildren would otherwise survive the timeout.
                if getattr(proc, "pid", None) is not None:
                    try:
                        os.killpg(os.getpgid(proc.pid), 15)
                        await asyncio.sleep(0.5)
                        os.killpg(os.getpgid(proc.pid), 9)
                    except (ProcessLookupError, PermissionError):
                        pass
                else:
                    proc.kill()
                await proc.wait()
                stdout, stderr = b"", b""

            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            artifacts = _artifacts(out_dir)
            subcall_info = ({"subcalls": {"used": subcall["used"],
                                          "max": subcall["max_calls"]}}
                            if subcall else {})
            stream_files = _spill_streams(out, err, out_dir)
            rc = 124 if timed_out else (proc.returncode or 0)

            result = {
                "stdout": out[-_OUT_CAP:],
                "stderr": err[-_ERR_CAP:] if err else "",
                "exit_code": rc,
                "ok": rc == 0 and not timed_out,
                **({"out_dir": str(out_dir), "written_files": artifacts}
                   if out_dir else {}),
                **stream_files,
                **subcall_info,
            }
            if timed_out:
                result["timed_out"] = True
                result["note"] = f"killed after {timeout}s"
            if sandbox_note:
                result["sandbox_warning"] = sandbox_note
            return ToolResult(status="ok", result=result)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _build_python_cmd(self, script: str, sandbox: str | None, workdir: Path,
                          out_dir: Path | None, python: str, rlimit_as_mb: int,
                          subcall_sock: str | None = None,
                          exec_work: Path | None = None) -> list[str]:
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
                cmd += _bind_rw(out_dir)            # artifact escape hatch
            if exec_work:
                # The run's persistent workspace (preamble chdir lands here).
                cmd += _bind_rw(exec_work)
            if subcall_sock:
                # Unix-socket mediation channel for llm_query — a filesystem
                # object, so --net=none above is unaffected. Connecting to a
                # unix socket needs write permission on its path.
                cmd += _bind_rw(subcall_sock)
            cmd += [
                f"--rlimit-as={rlimit_as_mb * 1024**2}",  # virtual address space cap
                "--rlimit-cpu=60",
                python, script,
            ]
            return cmd
        # Fallback: no sandbox — only reachable after human approval
        # (needs_confirmation) or when the operator disabled the sandbox. Logged.
        _log.warning("firejail unavailable — running code.run python WITHOUT sandbox")
        return [python, script]
