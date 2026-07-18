"""Managed child processes with log capture and auto-restart.

Launches long-running services (brain, embedding, reranker) as child processes,
captures their stdout/stderr into bounded ring buffers, and restarts on crash.

Usage:
    pm = ProcessManager()
    pm.add("brain", cmd="...", env={...}, restart=True)
    await pm.start_all()        # non-blocking, returns immediately
    pm.status()                 # {"brain": {"pid": 123, "alive": True, ...}}
    pm.logs("brain", lines=50)  # last 50 log lines
    await pm.stop_all()         # SIGKILL all, wait for exit
"""

from __future__ import annotations

import asyncio
import collections
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ManagedProcess:
    name: str
    command: str
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    restart: bool = True
    restart_delay: float = 5.0
    start_delay: float = 0.0      # delay before first launch (stagger GPU loads)
    max_restarts: int = 20
    kill_signal: int = signal.SIGKILL
    max_log_lines: int = 2000

    # Runtime state
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    log: collections.deque = field(default_factory=lambda: collections.deque(maxlen=2000), repr=False)
    started_at: float | None = None
    restarts: int = 0
    _task: asyncio.Task | None = field(default=None, repr=False)
    _stopping: bool = False


class ProcessManager:
    def __init__(self):
        self._procs: dict[str, ManagedProcess] = {}

    def add(self, name: str, command: str, *,
            env: dict[str, str] | None = None,
            cwd: str | None = None,
            restart: bool = True,
            restart_delay: float = 5.0,
            start_delay: float = 0.0,
            kill_signal: int = signal.SIGKILL,
            max_log_lines: int = 2000) -> None:
        self._procs[name] = ManagedProcess(
            name=name, command=command,
            env=env or {}, cwd=cwd,
            restart=restart, restart_delay=restart_delay,
            start_delay=start_delay,
            kill_signal=kill_signal,
            max_log_lines=max_log_lines,
        )
        self._procs[name].log = collections.deque(maxlen=max_log_lines)

    async def start_all(self) -> None:
        for name, mp in self._procs.items():
            if mp._task is None or mp._task.done():
                mp._stopping = False
                mp._task = asyncio.create_task(self._run_loop(mp))

    async def stop_all(self) -> None:
        for mp in self._procs.values():
            mp._stopping = True
            await self._kill(mp)
            if mp._task and not mp._task.done():
                mp._task.cancel()
                try:
                    await mp._task
                except (asyncio.CancelledError, Exception):
                    pass

    async def stop_one(self, name: str) -> bool:
        mp = self._procs.get(name)
        if not mp:
            return False
        mp._stopping = True
        await self._kill(mp)
        if mp._task and not mp._task.done():
            mp._task.cancel()
        return True

    async def start_one(self, name: str) -> bool:
        mp = self._procs.get(name)
        if not mp:
            return False
        mp._stopping = False
        mp.restarts = 0
        if mp._task is None or mp._task.done():
            mp._task = asyncio.create_task(self._run_loop(mp))
        return True

    def status(self) -> dict[str, dict]:
        out = {}
        for name, mp in self._procs.items():
            alive = mp.proc is not None and mp.proc.returncode is None
            out[name] = {
                "pid": mp.proc.pid if mp.proc else None,
                "alive": alive,
                "started_at": mp.started_at,
                "uptime_s": round(time.time() - mp.started_at, 1) if mp.started_at and alive else None,
                "restarts": mp.restarts,
                "exit_code": mp.proc.returncode if mp.proc and not alive else None,
                "stopping": mp._stopping,
                "command": mp.command[:200],
            }
        return out

    def logs(self, name: str, lines: int = 100) -> list[str]:
        mp = self._procs.get(name)
        if not mp:
            return []
        items = list(mp.log)
        return items[-lines:] if lines < len(items) else items

    def names(self) -> list[str]:
        return list(self._procs.keys())

    # --- internals ---

    async def _run_loop(self, mp: ManagedProcess) -> None:
        # Stagger start: wait before first launch (e.g. let brain load before coder)
        if mp.start_delay > 0 and mp.restarts == 0:
            mp.log.append(f"[pm] waiting {mp.start_delay}s before first start (start_delay)")
            await asyncio.sleep(mp.start_delay)
        while not mp._stopping:
            try:
                await self._spawn(mp)
                await self._read_output(mp)
                # Process exited
                code = mp.proc.returncode if mp.proc else -1
                mp.log.append(f"[pm] process exited with code {code}")
                if mp._stopping or not mp.restart:
                    break
                mp.restarts += 1
                if mp.restarts > mp.max_restarts:
                    mp.log.append(f"[pm] max restarts ({mp.max_restarts}) reached, giving up")
                    break
                mp.log.append(f"[pm] restarting in {mp.restart_delay}s (restart #{mp.restarts})")
                await asyncio.sleep(mp.restart_delay)
            except asyncio.CancelledError:
                await self._kill(mp)
                break
            except Exception as e:
                mp.log.append(f"[pm] error: {e}")
                if not mp._stopping:
                    await asyncio.sleep(mp.restart_delay)

    async def _spawn(self, mp: ManagedProcess) -> None:
        env = {**os.environ, **mp.env}
        mp.log.append(f"[pm] starting: {mp.command[:200]}")
        mp.proc = await asyncio.create_subprocess_shell(
            mp.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout
            env=env,
            cwd=mp.cwd,
            preexec_fn=os.setsid,  # new process group for clean kill
        )
        mp.started_at = time.time()
        mp.log.append(f"[pm] started pid={mp.proc.pid}")

    async def _read_output(self, mp: ManagedProcess) -> None:
        assert mp.proc and mp.proc.stdout
        while True:
            try:
                line = await mp.proc.stdout.readline()
            except (asyncio.CancelledError, Exception):
                break
            if not line:
                break  # EOF — process exited
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            mp.log.append(text)

    async def _kill(self, mp: ManagedProcess) -> None:
        if mp.proc and mp.proc.returncode is None:
            try:
                pgid = os.getpgid(mp.proc.pid)
                os.killpg(pgid, mp.kill_signal)
                mp.log.append(f"[pm] sent signal {mp.kill_signal} to pgid {pgid}")
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(mp.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                mp.log.append("[pm] force kill after timeout")
                try:
                    mp.proc.kill()
                except Exception:
                    pass
