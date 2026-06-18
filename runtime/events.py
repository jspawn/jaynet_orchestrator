"""Event bus — the seam between the agent loop and any UI.

The runtime emits structured events (transport-neutral dicts) through an
`on_event` callback. The web layer hands the loop a callback that publishes into
this bus; the loop never imports HTTP, SSE, or this module. Swap the bus for a
WebSocket fan-out, a log file, or Chainlit without touching loop.py.

Each run_id gets its own fan-out plus a small replay buffer so a browser that
(re)connects mid-run — or resumes via Last-Event-ID — still sees the steps it
missed.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict


class EventBus:
    def __init__(self, buffer_size: int = 500):
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._buffer: dict[str, list[dict]] = defaultdict(list)
        self._buffer_size = buffer_size

    async def publish(self, run_id: str, event: dict) -> None:
        buf = self._buffer[run_id]
        buf.append(event)
        if len(buf) > self._buffer_size:
            del buf[: len(buf) - self._buffer_size]
        for q in list(self._subs.get(run_id, ())):
            q.put_nowait(event)

    def subscribe(self, run_id: str, after_seq: int = 0) -> asyncio.Queue:
        """Return a queue pre-loaded with any buffered events after `after_seq`."""
        q: asyncio.Queue = asyncio.Queue()
        for e in self._buffer.get(run_id, ()):
            if e.get("seq", 0) > after_seq:
                q.put_nowait(e)
        self._subs[run_id].add(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        self._subs.get(run_id, set()).discard(q)

    def forget(self, run_id: str) -> None:
        """Drop the replay buffer for a finished run (call after a grace period)."""
        self._buffer.pop(run_id, None)
        self._subs.pop(run_id, None)
