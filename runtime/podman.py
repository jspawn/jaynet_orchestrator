"""One shared podman client call — sync, never raises.

A missing binary or a timeout comes back as rc 127 so callers fail/skip
cleanly. BLOCKING: async callers must go through asyncio.to_thread (a 60s
podman call on the web server's only event loop freezes the console and
stalls in-flight runs). The async variant with a scrubbed env lives in
tools/code/devbox.py (different contract: 3-tuple, no inherited secrets).
"""

from __future__ import annotations

import subprocess


def podman(*args: str, timeout: int = 60) -> tuple[int, bytes]:
    """(exit_code, combined_output) for one podman call."""
    try:
        proc = subprocess.run(["podman", *args], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
        return proc.returncode, proc.stdout or b""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e).encode()
