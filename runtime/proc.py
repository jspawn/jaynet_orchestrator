"""Shared async subprocess runner + tracked background tasks.

Two audit fixes live here (Sept 2026 code audit, items 9 and 10):

- `run()` — the ONE copy of the "spawn, communicate with a timeout, kill on
  timeout" pattern that used to be hand-written at ~17 call sites. The child
  always starts its own session (process group) and a timeout kills the
  WHOLE group — the direct child plus grandchildren like a `make test` or
  `sleep` it spawned — then reaps the child, so a timeout never leaves
  orphans holding the pipes open or a zombie behind. This is the pattern
  tools/code/run.py already used; the leaky sites (web/goals.py, tools/ops,
  tools/lint, h5i/graphify/benchlab, …) now delegate here.

- `spawn_background()` — `asyncio.create_task` without the footguns: the
  task is held in a module-level set (a bare create_task can be GC'd
  mid-run), a done callback logs any exception (silent task death is the
  other half of the bug), and `shutdown_background()` cancels + awaits
  everything still live from the server's shutdown path.

NOT for deliberate detached OS processes (subprocess.Popen with
start_new_session for service restarts) — those must outlive this process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Coroutine
from typing import Any

_log = logging.getLogger(__name__)

# Grace between SIGTERM and SIGKILL on the timeout path, and the bound on
# draining the pipes after the kill — same values tools/code/run.py used.
_TERM_GRACE_S = 0.5
_DRAIN_TIMEOUT_S = 5


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM, then SIGKILL the child's whole process group. Best-effort:
    a race with a natural exit (ProcessLookupError) or a foreign group
    (PermissionError) just ends the attempt."""
    pid = getattr(proc, "pid", None)
    if pid is None:
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        await asyncio.sleep(_TERM_GRACE_S)
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Group signal failed — at least take the direct child down.
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Drain the pipes and reap the child after a kill. Without this the
    dead child stays a zombie, and a grandchild that survived the kill would
    keep the pipes open — which is exactly the 30s hang this module exists
    to prevent, so the drain is itself bounded."""
    try:
        await asyncio.wait_for(proc.communicate(), timeout=_DRAIN_TIMEOUT_S)
    except Exception:
        pass


async def run(argv: list[str] | str, *,
              cwd: str | None = None,
              env: dict[str, str] | None = None,
              timeout: float | None = None,
              shell: bool = False,
              merge_stderr: bool = False) -> tuple[int, bytes, bytes]:
    """Run one command to completion; return (exit_code, stdout, stderr).

    `argv` is an argv list, or a command string with shell=True. stdin is
    always DEVNULL (these are unattended calls; nothing may block on input).
    With merge_stderr the child's stderr folds into stdout (stderr comes
    back as b""), matching create_subprocess_shell(STDOUT) callers.

    On timeout the whole process group is killed (SIGTERM, brief grace,
    SIGKILL) and the child reaped, THEN TimeoutError propagates — the
    caller words its own error message, and no child/grandchild survives.
    A cancelled caller kills the group too before the CancelledError
    propagates.
    """
    stderr_to = (asyncio.subprocess.STDOUT if merge_stderr
                 else asyncio.subprocess.PIPE)
    kwargs = {
        "cwd": cwd, "env": env,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": stderr_to,
        "stdin": asyncio.subprocess.DEVNULL,
        # Own session => own process group, so the timeout kill can take the
        # whole tree and a Ctrl-C of the server never hits the child.
        "start_new_session": True,
    }
    if shell:
        proc = await asyncio.create_subprocess_shell(str(argv), **kwargs)
    else:
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await _kill_group(proc)
        await _reap(proc)
        raise
    except asyncio.CancelledError:
        await _kill_group(proc)
        await _reap(proc)
        raise
    # Fakes in tests implement communicate() without a returncode.
    rc = getattr(proc, "returncode", 0)
    return (rc if rc is not None else 0, out or b"", err or b"")


# ---- tracked background tasks ------------------------------------------------

_BG_TASKS: set[asyncio.Task] = set()


def _on_done(task: asyncio.Task) -> None:
    _BG_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.error("background task %r died with %s: %s",
                   task.get_name(), type(exc).__name__, exc, exc_info=exc)


def spawn_background(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
    """create_task for fire-and-forget work, minus the footguns: a strong
    reference is held until completion (asyncio only weak-refs tasks, so an
    unreferenced one can be GC'd mid-run), the task is named for dumps and
    logs, any exception is logged on completion, and shutdown_background()
    cancels + awaits everything still tracked."""
    task = asyncio.create_task(coro, name=name)
    _BG_TASKS.add(task)
    task.add_done_callback(_on_done)
    return task


async def shutdown_background(timeout: float = 5.0) -> None:
    """Cancel every tracked task still running and await their exit. Called
    from the web server's shutdown path; `timeout` bounds the wait so one
    wedged task can't hang shutdown. Only same-loop tasks are touched:
    tracked tasks bound to another loop (a test artifact — the server runs
    one loop) must neither be cancelled from here nor gathered (gather over
    cross-loop tasks raises ValueError)."""
    loop = asyncio.get_running_loop()
    pending = [t for t in _BG_TASKS if not t.done()
               and t.get_loop() is loop]
    if not pending:
        return
    for t in pending:
        t.cancel()
    await asyncio.wait_for(
        asyncio.gather(*pending, return_exceptions=True), timeout=timeout)
