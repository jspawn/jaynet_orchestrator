"""Code execution tool.

Runs short Python snippets in a sandboxed subprocess. Default sandbox is firejail
(must be installed: `sudo pacman -S firejail`). Without firejail the call is
confirmation-gated; only after human approval does it fall back to a plain
subprocess (logged) — bare execution is never silent.

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

Container mode (eval harness only): when tools.code.container carries an {id,
workdir, python} mapping — injected exclusively by the eval runner through
run_overrides tools_patch for container eval cases (Terminal-Bench full
mode) — the snippet runs via `podman exec` inside that container instead of
firejail. The container is the sandbox; the run's work_root is bind-mounted
at the container workdir, so files persist across calls like a real terminal.
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

from runtime.tool_base import Tool, ToolContext, ToolResult, sandbox_missing, scrub_env

_PREAMBLE = """\
# Auto-injected preamble: bounded imports, no network, no fs writes outside cwd
import sys, os
sys.path = [p for p in sys.path if p and not p.startswith('/home')]
"""

_log = logging.getLogger(__name__)
# Container mode has no subcall seam (the mediation unix socket can't be
# mounted into a running container) — note it once per process, not per call.
_subcall_note_logged = False


def _container_cfg(cfg: dict) -> dict | None:
    """The per-run container binding ({id, workdir, python}) when the eval
    runner injected one via run_overrides tools_patch, else None. There is
    deliberately no static-config path that turns this on for chats."""
    ctr = cfg.get("container")
    if isinstance(ctr, dict) and str(ctr.get("id") or "").strip():
        return ctr
    return None


class CodeExecute(Tool):
    name = "code.execute"
    description = (
        "Execute a short Python snippet and return stdout. Sandboxed; "
        "no network, 30s timeout. Use for math, JSON manipulation, regex tests, "
        "quick computations, small plots. numpy/matplotlib are available (Agg "
        "backend): save files to the directory named by the ORCH_EXEC_OUT env "
        "var (os.environ['ORCH_EXEC_OUT']) — they're returned as written_files, "
        "hand them to the user with deliver.files. Print results to stdout. "
        "When the env var ORCH_SUBCALL_SOCK is set, the helpers "
        "llm_query(prompt, model=None, system=None) and "
        "llm_query_batched(prompts, ...) are pre-defined: mediated sub-LLM "
        "calls (billed to this run, capped per execution). Use them to map "
        "LLM work over SLICES of a large file instead of reading the whole "
        "file into your own context — chunk programmatically, llm_query_batched "
        "the chunks, reduce the answers yourself."
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

    def needs_confirmation(self, args: dict, ctx: ToolContext) -> bool:
        cfg = ctx.config.get("tools", {}).get("code", {})
        # Container mode (eval-runner managed, via run_overrides tools_patch
        # only): the container IS the sandbox — never gated.
        if _container_cfg(cfg) is not None:
            return False
        # Sandboxed (firejail) by default. If the operator disabled the sandbox
        # (sandbox: null/other) or firejail isn't installed, the snippet would
        # run bare on the host — gate that behind human approval instead of
        # silently degrading (same rule as code.run).
        sandbox = cfg.get("sandbox", "firejail")
        if sandbox != "firejail":
            return True
        return sandbox_missing(["firejail"]) is not None

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        code = args["code"]
        cfg = ctx.config.get("tools", {}).get("code", {})
        timeout = min(int(args.get("timeout_s", cfg.get("timeout_s", 30))), 60)
        container = _container_cfg(cfg)
        if container is not None:
            # Eval-runner container mode (Terminal-Bench full import): the
            # snippet runs inside the case's podman container, whose workdir
            # IS the run's work_root — state persists across calls, like a
            # real terminal. Reachable only via run_overrides tools_patch.
            return await self._execute_container(code, container, timeout, ctx)
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

        # Mediated sub-LLM seam (RLM primitive, runtime/subcall.py): when the
        # run offers grants, mint one per execution and hand the snippet the
        # unix-socket path + token plus the llm_query helpers. Transport is a
        # unix socket, so --net=none stays intact (the sandbox reaches it via
        # a --read-write exception on the socket path below). A grant failure
        # must not break code execution — the snippet just runs without helpers.
        subcall = None
        grant_fn = getattr(ctx, "subcall_grant", None)
        if grant_fn is not None:
            try:
                subcall = await grant_fn({})
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "subcall grant failed — code.execute runs without llm_query",
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
        # single BLAS thread keeps numpy's buffer/stack appetite inside the
        # rlimit (and the sandbox from burning all cores).
        env = scrub_env(dict(os.environ))
        env.setdefault("MPLBACKEND", "Agg")
        env.setdefault("OMP_NUM_THREADS", "1")
        env.setdefault("OPENBLAS_NUM_THREADS", "1")
        if out_dir:
            env["ORCH_EXEC_OUT"] = str(out_dir)
        if subcall:
            # Injected AFTER scrub_env on purpose: the per-execution token is
            # a bearer for the mediation socket, and scrub_env strips *_TOKEN.
            env["ORCH_SUBCALL_SOCK"] = subcall["sock"]
            env["ORCH_SUBCALL_TOKEN"] = subcall["token"]

        try:
            cmd = self._build_cmd(script, sandbox, workdir, out_dir,
                                  cfg.get("python", "python"),
                                  int(cfg.get("rlimit_as_mb", 1024)),
                                  subcall_sock=subcall["sock"] if subcall else None)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                env=env,
                start_new_session=True,   # own process group so we can kill the tree
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
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
                return ToolResult(status="error", result=None,
                                  error=f"execution timeout after {timeout}s")

            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            artifacts = self._artifacts(out_dir)
            subcall_info = ({"subcalls": {"used": subcall["used"],
                                          "max": subcall["max_calls"]}}
                            if subcall else {})

            if proc.returncode != 0:
                return ToolResult(status="error", result={
                    "stdout": out[-2000:], "stderr": err[-2000:],
                    "exit_code": proc.returncode,
                    **({"out_dir": str(out_dir), "written_files": artifacts}
                       if out_dir else {}),
                    **subcall_info,
                }, error=f"exit code {proc.returncode}")

            return ToolResult(status="ok", result={
                "stdout": out[-5000:],
                "stderr": err[-1000:] if err else "",
                "exit_code": 0,
                **({"out_dir": str(out_dir), "written_files": artifacts}
                   if out_dir else {}),
                **subcall_info,
            })
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _build_container_cmd(ctr_id: str, ctr_workdir: str, python: str,
                             script_rel: str, timeout: int,
                             env: dict) -> list[str]:
        """`podman exec` argv for one snippet run. coreutils `timeout` bounds
        the snippet INSIDE the container (the asyncio wait_for in
        _execute_container is the backstop against a stuck podman itself)."""
        cmd = ["podman", "exec", "--workdir", ctr_workdir]
        for k, v in env.items():
            cmd += ["--env", f"{k}={v}"]
        cmd += [ctr_id, "timeout", str(timeout), python, script_rel]
        return cmd

    async def _execute_container(self, code: str, container: dict,
                                 timeout: int, ctx: ToolContext) -> ToolResult:
        """Run the snippet inside the eval case's podman container. The runner
        bind-mounts the run's work_root at the container workdir, so the
        per-call script and every file the snippet writes are shared state —
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
        python = str(container.get("python") or "python3")
        work_root = Path(ctx.work_root)

        # The subcall/llm_query seam can't cross into a running container
        # (unix socket) — skip the grant silently; the snippet just runs
        # without the helpers.
        if getattr(ctx, "subcall_grant", None) is not None \
                and not _subcall_note_logged:
            _subcall_note_logged = True
            _log.info("code.execute container mode: subcall (llm_query) seam "
                      "unavailable — snippets run without helpers")

        script_name = f".orch-exec-{uuid.uuid4().hex[:12]}.py"
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
        try:
            script.write_text(_PREAMBLE + "\n" + textwrap.dedent(code),
                              encoding="utf-8")
            cmd = self._build_container_cmd(ctr_id, ctr_workdir, python,
                                            script_name, timeout, env_in)
            # Secrets stay out of the podman client process env; the snippet's
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
            artifacts = self._artifacts(out_dir)
            if proc.returncode != 0:
                return ToolResult(status="error", result={
                    "stdout": out[-2000:], "stderr": err[-2000:],
                    "exit_code": proc.returncode,
                    "out_dir": str(out_dir), "written_files": artifacts,
                }, error=f"exit code {proc.returncode}")
            return ToolResult(status="ok", result={
                "stdout": out[-5000:],
                "stderr": err[-1000:] if err else "",
                "exit_code": 0,
                "out_dir": str(out_dir), "written_files": artifacts,
            })
        finally:
            script.unlink(missing_ok=True)

    @staticmethod
    def _artifacts(out_dir: Path | None) -> list[str]:
        if not out_dir or not out_dir.exists():
            return []
        return sorted(str(p) for p in out_dir.iterdir() if p.is_file())

    def _build_cmd(self, script: str, sandbox: str | None, workdir: Path,
                   out_dir: Path | None, python: str, rlimit_as_mb: int,
                   subcall_sock: str | None = None) -> list[str]:
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
            if subcall_sock:
                # Unix-socket mediation channel for llm_query — a filesystem
                # object, so --net=none above is unaffected. Connecting to a
                # unix socket needs write permission on its path.
                cmd.append(f"--read-write={subcall_sock}")
            cmd += [
                f"--rlimit-as={rlimit_as_mb * 1024**2}",  # virtual address space cap
                "--rlimit-cpu=60",
                python, script,
            ]
            return cmd
        # Fallback: no sandbox — only reachable after human approval
        # (needs_confirmation) or when the operator disabled the sandbox. Logged.
        import logging
        logging.warning("firejail unavailable — running code.execute WITHOUT sandbox")
        return [python, script]
