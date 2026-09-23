"""runtime.proc — the shared subprocess runner (process-group kill on
timeout, no orphans, no zombies) and the tracked background-task helper
(spawn_background / shutdown_background)."""
from __future__ import annotations

import asyncio
import logging
import os
import time

import pytest
from conftest import run

from runtime import proc as P


def _dead(pid: int, within: float = 3.0) -> bool:
    """Poll until the pid is fully gone (reaped — a zombie still 'exists'
    for os.kill(pid, 0), so this passing means no zombie AND no orphan)."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


# ---- run(): capture + exit codes ----------------------------------------------


def test_run_captures_streams_and_exit_code():
    rc, out, err = run(P.run(["bash", "-c", "echo hi; echo oops >&2; exit 3"]))
    assert rc == 3
    assert out == b"hi\n"
    assert err == b"oops\n"


def test_run_shell_with_env_and_merged_stderr():
    rc, out, err = run(P.run(
        "echo val=$FOO; echo oops >&2", shell=True, merge_stderr=True,
        env={"FOO": "bar", "PATH": os.environ.get("PATH", "")}))
    assert rc == 0
    assert b"val=bar" in out and b"oops" in out
    assert err == b""


def test_run_missing_program_raises_oserror():
    with pytest.raises(OSError):
        run(P.run(["/no/such/binary-xyz"]))


# ---- run(): timeout / cancellation kill the whole tree -------------------------


def test_timeout_kills_process_group_and_reaps(tmp_path):
    """The timeout path SIGTERMs/SIGKILLs the child's whole process group —
    a grandchild (`sleep 60`) must not survive holding the pipes (the h5i
    30s-hang bug), and the direct child must be reaped, not zombied."""
    child_pid = tmp_path / "child.pid"
    grand_pid = tmp_path / "grand.pid"
    script = (f"echo $$ > {child_pid}; sleep 60 & echo $! > {grand_pid}; wait")
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        run(P.run(["bash", "-c", script], timeout=1))
    assert time.monotonic() - t0 < 15      # never waits out the 60s sleeps
    pid = int(child_pid.read_text())
    gpid = int(grand_pid.read_text())
    assert _dead(pid), "direct child survived (or was left a zombie)"
    assert _dead(gpid), "grandchild survived the timeout — group not killed"


def test_cancellation_kills_process_group(tmp_path):
    grand_pid = tmp_path / "grand.pid"
    script = f"sleep 60 & echo $! > {grand_pid}; wait"

    async def main():
        task = asyncio.create_task(P.run(["bash", "-c", script]))
        for _ in range(200):               # poll for the grandchild's pid file
            if grand_pid.exists():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(main())
    assert _dead(int(grand_pid.read_text()))


# ---- spawn_background / shutdown_background ------------------------------------


def test_spawn_background_tracks_and_logs_failure(caplog):
    async def main():
        async def boom():
            raise ValueError("kaboom")
        t = P.spawn_background(boom(), name="test-boom")
        assert t in P._BG_TASKS
        await asyncio.sleep(0.1)           # let it finish + done callback run
        assert t.done()
        assert t not in P._BG_TASKS        # discarded on completion

    with caplog.at_level(logging.ERROR, logger="runtime.proc"):
        run(main())
    assert any("test-boom" in r.getMessage() for r in caplog.records)
    assert any(r.exc_info and isinstance(r.exc_info[1], ValueError)
               for r in caplog.records)


def test_spawn_background_success_is_silent(caplog):
    async def main():
        async def ok():
            return 42
        t = P.spawn_background(ok(), name="test-ok")
        assert await t == 42
        await asyncio.sleep(0.05)
        assert t not in P._BG_TASKS

    with caplog.at_level(logging.ERROR, logger="runtime.proc"):
        run(main())
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_shutdown_background_cancels_and_awaits():
    async def main():
        started = asyncio.Event()

        async def long():
            started.set()
            await asyncio.sleep(60)

        t = P.spawn_background(long(), name="test-long")
        await started.wait()
        assert t in P._BG_TASKS
        await P.shutdown_background()
        await asyncio.sleep(0.05)          # flush done callbacks
        assert t.cancelled()
        assert not P._BG_TASKS

    run(main())
