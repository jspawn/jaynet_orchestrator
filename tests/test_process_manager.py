"""Managed-process argv, long-line output, and process-group kill regressions."""
import asyncio
import os
import shlex
import signal
import sys

from runtime.process_manager import ProcessManager


def test_oversized_log_line_is_drained_and_process_reaped():
    async def main():
        pm = ProcessManager()
        pm.add("long-line", shlex.join([
            sys.executable, "-c", "print('x' * 200000)"]), restart=False)
        mp = pm._procs["long-line"]
        await pm.start_one("long-line")
        await asyncio.wait_for(mp._task, timeout=5)
        assert mp.proc.returncode == 0
        assert any(len(line) == 200000 for line in mp.log)

    asyncio.run(main())


def test_sigterm_timeout_escalates_to_process_group(monkeypatch):
    class _Proc:
        pid = 123
        returncode = None

        def __init__(self):
            self.waits = 0

        async def wait(self):
            self.waits += 1
            if self.waits == 1:
                raise TimeoutError
            self.returncode = -9

        def kill(self):
            self.returncode = -9

    signals = []
    monkeypatch.setattr(os, "getpgid", lambda pid: 456)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    pm = ProcessManager()
    pm.add("stubborn", "unused", kill_signal=signal.SIGTERM)
    mp = pm._procs["stubborn"]
    mp.proc = _Proc()
    asyncio.run(pm._kill(mp))
    assert (456, signal.SIGKILL) in signals
    assert mp.proc.waits == 2
