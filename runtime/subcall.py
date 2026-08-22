"""Mediated sub-LLM calls from inside code.execute — the RLM primitive.

The Recursive-Language-Model pattern (arxiv.org/abs/2512.24601) keeps the long
prompt OUT of the context window as an addressable object and maps sub-LLM
calls over programmatic slices of it. JayNet already had the object (workspace
files) and the slicer (code.execute); the missing piece was a *mediated*
sub-LLM call from inside the sandbox. This module is that piece.

Transport: a per-run UNIX socket server (started lazily by AgentRuntime.run on
the first grant). Unix sockets are filesystem objects, so the firejail
`--net=none` posture stays fully intact — the snippet reaches the socket via a
`--read-write=` exception on the socket path, never via the network. The
client side is a stdlib-only preamble injected into the snippet (see
CLIENT_PREAMBLE) defining `llm_query` / `llm_query_batched`.

Mediation (the point of the exercise — an unmediated loop would bypass every
guarantee the harness owns):
- **Auth**: per-execution grants (random token + call cap) minted by
  code.execute via the ctx.subcall_grant seam; unknown/exhausted tokens are
  refused.
- **Budget**: every subcall is charged to the RUN's Budget (tokens + cost via
  the shared cost table) and the live cost meter; a run that already hit a
  ceiling gets refusals, not more spend.
- **Taint**: a private-tainted run may only call LOCAL aliases (hard refuse —
  a sandboxed snippet cannot ask the human the way ctx.spawn can, so this
  mirrors cloud_gate.privacy_refusal's fail-safe, never the confirm gate).
- **Model policy**: default is the run's own brain; an explicit `model` must
  be the run's model or a local alias. Arbitrary cloud aliases are refused —
  cloud fan-out from model-written code stays a human-gated agent.spawn thing.
- **Trace**: each subcall lands in trace.db as a `subcall` event (model,
  sizes, usage, latency), so replay shows exactly what the snippet asked.
- **Caps**: max calls per execution, concurrency bound per run, per-call
  timeout, prompt/output size caps. Model-written code multiplying LLM calls
  is the main risk; these are the bounds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path

from runtime.cloud_gate import is_local_alias
from runtime.model_client import _strip_think
from runtime.paths import SANDBOX_DIR

log = logging.getLogger(__name__)

# Hard transport cap on one request line (prompt + framing). The policy cap
# (max_prompt_chars) is the meaningful limit; this just bounds the read buffer.
_MAX_REQUEST_BYTES = 2 * 1024 * 1024


# Injected into the snippet AFTER the standard preamble, only when the run
# granted subcalls (env vars present). Stdlib only, no eager connections.
# NOTE: keep this dependency-free — the sandboxed interpreter may be a minimal
# system python. \\n escapes produce literal backslash-n in the snippet source.
CLIENT_PREAMBLE = '''
# Orchestrator-injected mediated sub-LLM helpers. Calls are billed to this
# run's budget and capped per execution — batch with llm_query_batched instead
# of looping one-by-one, and keep prompts as SLICES, not whole files.
import os as _sc_os, json as _sc_json, socket as _sc_socket

def llm_query(prompt, model=None, system=None, max_tokens=None, timeout_s=300):
    """One mediated sub-LLM completion. Returns the answer text (str)."""
    req = {"token": _sc_os.environ.get("ORCH_SUBCALL_TOKEN", ""),
           "prompt": prompt}
    if model: req["model"] = model
    if system: req["system"] = system
    if max_tokens: req["max_tokens"] = int(max_tokens)
    path = _sc_os.environ.get("ORCH_SUBCALL_SOCK")
    if not path:
        raise RuntimeError("llm_query unavailable: this run granted no subcalls")
    s = _sc_socket.socket(_sc_socket.AF_UNIX, _sc_socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect(path)
        s.sendall((_sc_json.dumps(req) + "\\n").encode())
        buf = b""
        while not buf.endswith(b"\\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    resp = _sc_json.loads(buf.decode() or "{}")
    if resp.get("status") != "ok":
        raise RuntimeError("llm_query: " + str(resp.get("error") or "unknown error"))
    return resp.get("text") or ""

def llm_query_batched(prompts, model=None, system=None, max_tokens=None, workers=4):
    """Map llm_query over prompts concurrently; returns answers in order."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 8))) as ex:
        return list(ex.map(lambda p: llm_query(
            p, model=model, system=system, max_tokens=max_tokens), prompts))
'''


def _err(msg: str) -> dict:
    return {"status": "error", "error": msg}


class SubcallServer:
    """Per-run unix-socket server mediating llm_query calls from sandboxes.

    Owned by AgentRuntime.run: constructed lazily on the first grant, closed
    on run end. `runtime` is the AgentRuntime (used for _model_turn +
    cost_table); `tainted`/`budget`/`emit`/`emit_cost` are the run's live
    objects so accounting and privacy see the current state, not a snapshot.
    """

    def __init__(self, runtime, *, run_id: str, config: dict,
                 default_model: str, tainted, budget, emit, emit_cost):
        self.runtime = runtime
        self.run_id = run_id
        self.config = config
        self.default_model = default_model
        self.tainted = tainted                # callable() -> bool (live view)
        self.budget = budget
        self.emit = emit                      # async (type, iteration, data)
        self.emit_cost = emit_cost            # async (model, delta_usd)
        sc = ((config.get("tools") or {}).get("code") or {}).get("subcalls") or {}
        self.max_calls = self._int(sc.get("max_calls"), 64)
        self.timeout_s = self._float(sc.get("timeout_s"), 240.0)
        self.max_output_tokens = self._int(sc.get("max_output_tokens"), 4096)
        self.max_prompt_chars = self._int(sc.get("max_prompt_chars"), 400_000)
        self._sem = asyncio.Semaphore(max(1, self._int(sc.get("max_concurrent"), 4)))
        self._grants: dict[str, dict] = {}
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()
        self.sock_path: str | None = None

    @staticmethod
    def _int(v, default: int) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float(v, default: float) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    async def start(self) -> None:
        SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
        path = SANDBOX_DIR / f"subcall-{self.run_id[:8]}-{secrets.token_hex(4)}.sock"
        self._server = await asyncio.start_unix_server(
            self._accept, path=str(path), limit=_MAX_REQUEST_BYTES)
        self.sock_path = str(path)

    def mint_grant(self, max_calls: int | None = None) -> dict:
        """One per code.execute call. The returned dict IS the tracked record —
        the caller reads `used` after the snippet exits for its own report."""
        grant = {"sock": self.sock_path, "token": secrets.token_hex(16),
                 "max_calls": max_calls or self.max_calls, "used": 0}
        self._grants[grant["token"]] = grant
        return grant

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self.sock_path:
            try:
                Path(self.sock_path).unlink(missing_ok=True)
            except OSError:
                pass

    # ---- connection handling -------------------------------------------------

    def _accept(self, reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter) -> None:
        t = asyncio.ensure_future(self._handle(reader, writer))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=30)
            except (TimeoutError, ValueError):
                line = b""
            if not line:
                resp = _err("empty or oversized subcall request")
            else:
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    resp = _err("malformed subcall request (not JSON)")
                else:
                    resp = await self._serve(req)
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # never wedge the snippet
            log.exception("subcall handler failed")
            resp = _err(f"internal subcall error: {type(e).__name__}")
        try:
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        try:
            writer.close()
        except OSError:
            pass

    # ---- policy + the call itself ---------------------------------------------

    def _resolve_model(self, requested: str | None) -> tuple[str | None, dict | None]:
        model = requested or self.default_model
        if model != self.default_model and not is_local_alias(model, self.config):
            return None, _err(
                f"model '{model}' is not allowed from llm_query — subcalls use "
                f"the run's own model ('{self.default_model}') or a local alias; "
                "for a cloud model, ask the orchestrator to use agent.spawn instead")
        if self.tainted() and not is_local_alias(model, self.config):
            return None, _err(
                "blocked by privacy: this run holds private tool results, so "
                "llm_query is restricted to local models. Re-run without a "
                "model override, or use a local alias.")
        return model, None

    async def _serve(self, req: dict) -> dict:
        if not isinstance(req, dict):
            return _err("subcall request must be a JSON object")
        grant = self._grants.get(str(req.get("token") or ""))
        if grant is None:
            return _err("unknown or expired subcall token")
        if grant["used"] >= grant["max_calls"]:
            return _err(
                f"subcall cap reached for this execution ({grant['max_calls']}) — "
                "use larger chunks, or aggregate incrementally instead of one "
                "call per line")
        prompt = req.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return _err("llm_query needs a non-empty 'prompt' string")
        if len(prompt) > self.max_prompt_chars:
            return _err(
                f"prompt too large ({len(prompt)} chars > {self.max_prompt_chars}) — "
                "slice smaller; the point is addressing chunks, not hauling bulk")
        system = req.get("system")
        if system is not None and not isinstance(system, str):
            return _err("'system' must be a string")
        system = (system or "")[:4000]
        model, refusal = self._resolve_model(req.get("model"))
        if refusal is not None:
            return refusal
        try:
            max_tokens = min(self._int(req.get("max_tokens"), self.max_output_tokens),
                             self.max_output_tokens)
        except (TypeError, ValueError):
            max_tokens = self.max_output_tokens
        # A run that already hit a ceiling gets refusals, not more spend —
        # the loop's next tick would end it anyway; failing fast here keeps
        # the snippet from burning wall clock on calls that can't be billed.
        from runtime.budget import BudgetExceeded
        try:
            self.budget.check()
        except BudgetExceeded as e:
            return _err(f"run budget exhausted ({e.reason}) — no further subcalls")

        grant["used"] += 1                         # failed calls still bill
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        started = time.monotonic()
        try:
            async with self._sem:
                turn = await asyncio.wait_for(
                    self.runtime._model_turn(
                        messages, [], model=model, think=False,
                        sampling={"max_tokens": max_tokens}),
                    timeout=self.timeout_s or None)
        except TimeoutError:
            return _err(f"subcall timed out after {self.timeout_s:g}s")
        except Exception as e:
            # ModelTurnStalled, LiteLLM HTTP errors, … — surface as data so the
            # snippet can react (retry with a smaller chunk) instead of dying.
            return _err(f"subcall model turn failed: {e}")

        text = _strip_think((turn.get("message") or {}).get("content") or "")
        usage = turn.get("usage") or {}
        latency_ms = int((time.monotonic() - started) * 1000)
        before = self.budget.cost_usd
        self.budget.add_usage(
            model,
            prompt=usage.get("prompt_tokens", 0),
            completion=usage.get("completion_tokens", 0),
            cached=(usage.get("prompt_tokens_details", {}) or {}).get("cached_tokens", 0)
                   if isinstance(usage.get("prompt_tokens_details"), dict) else 0,
            cost_table=self.runtime.cost_table,
        )
        await self.emit_cost(model, self.budget.cost_usd - before)
        await self.emit("subcall", self.budget.iterations, {
            "model": model,
            "prompt_chars": len(prompt),
            "completion_chars": len(text),
            "usage": usage,
            "latency_ms": latency_ms,
            "call": grant["used"],
            "calls_max": grant["max_calls"],
        })
        await self.emit("progress", self.budget.iterations, {
            "label": f"llm_query {grant['used']}/{grant['max_calls']} → {model}",
            "type": "tool", "ok": True})
        return {"status": "ok", "text": text, "usage": usage,
                "calls_remaining": grant["max_calls"] - grant["used"]}


def sweep_stale_sockets() -> int:
    """Remove subcall-*.sock files whose server is gone (a process kill skips
    close()). A live socket still accepts a connection — probe first, so
    sockets of runs alive in OTHER processes survive the sweep."""
    import socket as _s
    if not SANDBOX_DIR.is_dir():
        return 0
    removed = 0
    for p in SANDBOX_DIR.glob("subcall-*.sock"):
        probe = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(p))
        except OSError:
            p.unlink(missing_ok=True)
            removed += 1
        finally:
            probe.close()
    return removed
