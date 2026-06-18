"""Confirmation providers — how a human approves a gated tool call.

The loop decides *whether* approval is needed (confirmation.enabled, the tool's
requires_confirmation flag, auto_confirm). The provider decides *how* to ask:

- No provider  -> loop's built-in TTY prompt / non_interactive fallback (CLI).
- WebConfirmationProvider -> emit a `confirmation_request` event and await a
  Future that an HTTP endpoint resolves.

A provider gets the per-run `emit` so its request flows through the same event
pipeline (and trace log) as everything else.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Awaitable, Callable, Protocol

EmitFn = Callable[[str, int, dict], Awaitable[None]]


class ConfirmationProvider(Protocol):
    async def confirm(self, run_id: str, tool_name: str, args: dict,
                      emit: EmitFn) -> bool:
        ...


class WebConfirmationProvider:
    """Emits a confirmation_request event and waits for /approve to resolve it.

    `pending` is shared with the HTTP layer: it maps (run_id, confirmation_id) to
    the Future the request is blocked on. The approve endpoint sets its result.
    """

    def __init__(self, pending: dict[tuple[str, str], asyncio.Future],
                 timeout_s: float = 300.0, on_timeout: bool = False):
        self.pending = pending
        self.timeout_s = timeout_s
        self.on_timeout = on_timeout

    async def confirm(self, run_id: str, tool_name: str, args: dict,
                      emit: EmitFn) -> bool:
        cid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending[(run_id, cid)] = fut
        await emit("confirmation_request", 0, {
            "confirmation_id": cid, "tool": tool_name, "args": args,
            "timeout_s": self.timeout_s,
        })
        try:
            return bool(await asyncio.wait_for(fut, timeout=self.timeout_s))
        except asyncio.TimeoutError:
            return self.on_timeout
        finally:
            self.pending.pop((run_id, cid), None)

    def resolve(self, run_id: str, confirmation_id: str, approved: bool) -> bool:
        fut = self.pending.get((run_id, confirmation_id))
        if fut is not None and not fut.done():
            fut.set_result(bool(approved))
            return True
        return False
