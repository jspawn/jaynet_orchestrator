"""Model-call plumbing for the agent loop.

Everything that talks to LiteLLM: request-body construction (sampler params,
the local-only jinja thinking switch), the shared keep-alive httpx client,
per-backend concurrency gates, and the two model-turn paths (buffered and
streaming) with their stall/timeout watchdogs.

Split out of runtime/loop.py — AgentRuntime composes this via ModelClientMixin,
so the host class must provide: self.config, self.model, self.litellm_base,
self._local_concurrency, self._model_sems, self._local_aliases,
self._think_switch_aliases.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import httpx

log = logging.getLogger(__name__)

# Qwen3-family brains wrap chain-of-thought in <think>…</think>. That reasoning
# must never reach the user's answer, the conversation history, or the trace as
# answer text — it belongs in the UI's collapsible "thinking" view (routed live
# via the "reasoning" token scope). These helpers strip/split it.
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


_SAMPLER_KEYS = ("temperature", "top_p", "top_k", "min_p", "repeat_penalty",
                 "presence_penalty", "frequency_penalty", "seed", "max_tokens")


class _NullAsyncCtx:
    """No-op async context manager — stand-in when a model call is ungated
    (cloud aliases, or a local backend with no configured concurrency limit)."""
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False


_NULL_ASYNC_CTX = _NullAsyncCtx()


class ModelTurnStalled(Exception):
    """A model turn hit its liveness bound: the stall watchdog saw no streamed
    output for budgets.stall_s (zombie backend), or the turn ran past the total
    orchestrator.turn_timeout_s cap. Raised from the model-turn paths; the loop
    ends the run gracefully as "stalled" — partial results and trajectory are
    preserved, exactly like the budget_exceeded path."""


def _sampler_body(sampling: dict | None) -> dict:
    """Whitelist sampler params for the /v1/chat/completions body, dropping
    None/unset keys. An empty/None input yields {} — i.e. send no sampler params,
    so the server falls back to the model's own (preset) defaults."""
    s = sampling or {}
    return {k: s[k] for k in _SAMPLER_KEYS if s.get(k) is not None}


def _is_local_model(model: str | None,
                    extra_local: frozenset = frozenset()) -> bool:
    """True for local llama.cpp aliases (local-orchestrator, local-specialist, …).

    Only these honor `chat_template_kwargs` (the jinja thinking switch). Cloud
    providers reject unknown params — Anthropic 400s with "Extra inputs are not
    permitted" — so that key must never be sent to a cloud model. `extra_local`
    covers local aliases without the local- prefix — by convention the keys of
    orchestrator.local_concurrency (add a serve.start'd model there when it is
    registered under a custom alias).
    """
    return bool(model) and (model.startswith("local-") or model in extra_local)


def _turn_body(model: str, messages: list[dict], tools_schema: list[dict],
               sampling: dict | None, think: bool, stream: bool,
               extra_local: frozenset = frozenset(),
               think_switch: frozenset | None = None,
               reasoning_budget: int | None = None) -> dict:
    """Build the /v1/chat/completions body shared by both model-turn paths.

    `chat_template_kwargs` (the llama.cpp jinja thinking switch) is added ONLY for
    local models; cloud sub-agents run at the provider's default thinking mode
    (any reasoning is stripped from the answer downstream). `think_switch`
    narrows further which local aliases understand the kwarg — adopted vLLM /
    Ollama endpoints are excluded unless the admin opts in via preset caps
    (None = every local alias, the llama-only behavior).

    `reasoning_budget_tokens` (llama.cpp force-closes the think block at the
    budget, verified live: engages per-request even where the --reasoning-budget
    server flag did not) follows the same local-only gate — cloud providers
    reject unknown params. Capping thinking makes overthinking VISIBLE content
    the loop guards can nudge, instead of invisible reasoning burning the whole
    completion cap (live: 2x8192 tokens, empty answer).
    """
    body: dict = {
        "model": model,
        "messages": messages,
        "tools": tools_schema,
        "tool_choice": "auto",
        **_sampler_body(sampling),
    }
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    if _is_local_model(model, extra_local):
        if think_switch is None or model in think_switch:
            body["chat_template_kwargs"] = {"enable_thinking": think}
        if reasoning_budget:
            body["reasoning_budget_tokens"] = reasoning_budget
    return body


def _strip_think(text: str) -> str:
    """Remove complete <think>…</think> blocks from a finished string. Used on the
    non-streaming path and as a safety net on the assembled streaming content.
    An UNTERMINATED <think> (a truncated turn) is stripped to end-of-string too —
    otherwise raw chain-of-thought leaks into the answer."""
    if not text or _THINK_OPEN not in text:
        return text
    out = _THINK_RE.sub("", text)
    idx = out.find(_THINK_OPEN)
    if idx != -1:
        out = out[:idx]
    return out.strip()


def _suffix_prefix_len(s: str, tag: str) -> int:
    """Longest suffix of s that is a proper prefix of tag — i.e. how many trailing
    chars to hold back in case a tag is split across streamed chunks."""
    for k in range(min(len(s), len(tag) - 1), 0, -1):
        if s[-k:] == tag[:k]:
            return k
    return 0


_TOOLCALL_JSON_NUDGE = (
    "Your previous response was rejected by the server: a tool call's "
    "arguments were not valid JSON (a long string lost its closing quote). "
    "Re-emit your response — and keep each tool-call argument SHORT: write "
    "large file contents as several smaller fs.write calls, not one big "
    "argument.")


def _is_toolcall_json_500(status: int, body: str) -> bool:
    """llama.cpp parses tool-call arguments server-side and answers HTTP 500
    when the model's argument string isn't valid JSON (unterminated string,
    bad escaping — almost always a multi-KB fs.write payload). Distinct from
    other 500s: the generation is discarded, so a nudged retry is safe."""
    return status == 500 and "tool call" in body and "parse" in body


class _ToolcallJson500(Exception):
    """Internal signal: streaming turn died on a malformed tool-call JSON 500 —
    the wrapper retries it once with _TOOLCALL_JSON_NUDGE."""


class ModelClientMixin:
    """The LiteLLM-facing half of AgentRuntime (see module docstring for the
    attributes the host class must provide)."""
    @property
    def _think_aliases(self) -> frozenset | None:
        # Test harnesses and embedders that bypass AgentRuntime.__init__ get
        # the legacy behavior (every local alias receives the thinking switch).
        return getattr(self, "_think_switch_aliases", None)

    @property
    def _reasoning_budget(self) -> int | None:
        # Per-request thinking cap, read LIVE from config each turn (like the
        # other orchestrator.* knobs) so the admin Config tab override
        # hot-applies without a restart. 0/unset = send nothing.
        try:
            v = int(((getattr(self, "config", None) or {})
                     .get("orchestrator") or {})
                    .get("reasoning_budget_tokens", 0) or 0)
        except (TypeError, ValueError):
            v = 0
        return v or None

    def _model_sem(self, model: str):
        """Concurrency gate (asyncio.Semaphore) for in-flight calls to `model`,
        or None if unbounded. Local backends map to their server's slot count;
        cloud aliases are unset → None → real off-box parallelism is unthrottled.
        The gate wraps a single call only (not the agent loop), so a parent that
        spawns children has already released its slot before awaiting them."""
        limit = self._local_concurrency.get(model)
        if not isinstance(limit, int) or limit <= 0:
            return None
        sem = self._model_sems.get(model)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            self._model_sems[model] = sem
        return sem

    def _turn_timeout_s(self) -> float:
        """Total cap for ONE model turn (orchestrator.turn_timeout_s, default 900).
        Local models generate ~40 tok/s, so a legitimately long turn needs many
        minutes — this is a hang backstop, not a pacing limit. 0 disables."""
        raw = (self.config.get("orchestrator") or {}).get("turn_timeout_s", 900)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 900.0

    def _stall_s(self) -> float:
        """Silence watchdog for STREAMING model turns (budgets.stall_s, default
        180): no SSE line at all for this long — or only keepalive/empty
        traffic with no content/tool-call delta — means the backend is hung
        (zombie). Applies to model turns ONLY — never during tool execution,
        where a long silent stretch is legitimate (a code.delegate child can
        run for many quiet minutes). 0 disables."""
        raw = (self.config.get("budgets") or {}).get("stall_s", 180)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 180.0

    def _http_client(self) -> httpx.AsyncClient:
        """The shared keep-alive client for model turns. Created lazily (tests
        monkeypatch httpx.AsyncClient, so construction must happen at call
        time, not in __init__) and reused across turns — one connection pool
        per runtime instead of a fresh TCP handshake per model turn."""
        c = getattr(self, "_http", None)
        if c is None:
            c = httpx.AsyncClient()
            self._http = c
        return c

    def _retry_delays_s(self) -> list[float]:
        """Backoff between retried model turns on transient backend failures
        (orchestrator.transport_retry_delays_s, default [2, 5, 10]). A proxy
        restart's dead window is ~15s; the default spans it (readiness audit
        BE-3: un-retried ConnectErrors killed 172 runs in 14 days)."""
        raw = (self.config.get("orchestrator") or {}).get(
            "transport_retry_delays_s", [2, 5, 10])
        try:
            return [max(0.0, float(d)) for d in raw]
        except (TypeError, ValueError):
            return [2.0, 5.0, 10.0]

    @staticmethod
    def _is_retryable_transport(exc: BaseException) -> bool:
        """Transient backend failures worth a backoff retry: the proxy down/
        restarting (ConnectError/ConnectTimeout), a dropped connection
        (ReadError/RemoteProtocolError), or a 5xx from proxy/backend. NOT
        timeouts of a live turn (those are stalls) and NOT 4xx, and never
        the deterministic malformed-tool-call double failure."""
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout,
                            httpx.ReadError, httpx.RemoteProtocolError)):
            return True
        if isinstance(exc, RuntimeError):
            msg = str(exc)
            # The deterministic malformed-tool-call double failure is NOT a
            # transient blip (llama.cpp's parse error says "tool call", the
            # rescue's own hard error says "tool-call" — exclude both).
            low = msg.lower()
            return (msg.startswith("LiteLLM 5")
                    and "tool-call" not in low and "tool call" not in low)
        return False

    async def _model_turn(self, messages: list[dict], tools_schema: list[dict],
                          model: str | None = None, think: bool = True,
                          sampling: dict | None = None,
                          _retried_toolcall: bool = False) -> dict:
        """One call to a model via LiteLLM (local brain or a cloud sub-agent)."""
        model = model or self.model
        body = _turn_body(model, messages, tools_schema, sampling, think,
                          stream=False, extra_local=self._local_aliases,
                          think_switch=self._think_aliases,
                          reasoning_budget=self._reasoning_budget)
        timeout_s = self._turn_timeout_s()
        guard = self._model_sem(model) or _NULL_ASYNC_CTX
        delays = self._retry_delays_s()
        attempt = 0
        while True:
            try:
                # Only the POST itself holds the model semaphore: the
                # 400-handling (incl. the toolcall-JSON retry below) runs
                # AFTER release, so a retry can re-acquire — nesting the
                # recursive call inside the guard would deadlock a limit=1
                # semaphore.
                async with guard:
                    r = await self._http_client().post(
                        f"{self.litellm_base}/v1/chat/completions",
                        json=body,
                        headers=self._auth_headers(),
                        timeout=timeout_s or None,
                    )
            except httpx.ConnectTimeout:
                # A TIMEOUT subclass — must precede TimeoutException: an
                # unreachable proxy (restart window) is retryable, not a
                # stalled live turn.
                if attempt >= len(delays):
                    raise
                log.warning("model turn connect timeout for %s — retrying "
                            "in %gs", model, delays[attempt])
                await asyncio.sleep(delays[attempt])
                attempt += 1
                continue
            except httpx.TimeoutException:
                # No token heartbeat exists on this path — the total turn
                # timeout is its only liveness bound, so expiry means
                # "stalled", not an error.
                raise ModelTurnStalled(
                    f"model turn exceeded the {timeout_s:g}s total turn timeout "
                    "(orchestrator.turn_timeout_s); ending the run with work so far "
                    "preserved") from None
            except httpx.HTTPError as e:
                # Transport failure before/without a response (proxy restart,
                # dropped connection): retry with backoff, then surface.
                if not self._is_retryable_transport(e) or attempt >= len(delays):
                    raise
                log.warning("model turn transport failure for %s (%s) — "
                            "retrying in %gs", model, e, delays[attempt])
                await asyncio.sleep(delays[attempt])
                attempt += 1
                continue
            try:
                return await self._handle_turn_response(
                    r, messages, tools_schema, model, think, sampling,
                    _retried_toolcall)
            except RuntimeError as e:
                if not self._is_retryable_transport(e) or attempt >= len(delays):
                    raise
                log.warning("model turn backend 5xx for %s — retrying in %gs",
                            model, delays[attempt])
                await asyncio.sleep(delays[attempt])
                attempt += 1
                continue

    async def _handle_turn_response(self, r, messages, tools_schema, model,
                                    think, sampling, _retried_toolcall) -> dict:
        """Status handling + payload assembly for one finished POST (split out
        of _model_turn so transport retries wrap the whole exchange)."""
        if r.status_code >= 400:
            # Surface the proxy's actual explanation instead of a bare code.
            body_txt = r.text[:1000]
            log.error("model turn failed: HTTP %s from %s — %s",
                      r.status_code, model, body_txt)
            if not _retried_toolcall and _is_toolcall_json_500(
                    r.status_code, body_txt):
                # llama.cpp parses tool-call args server-side and 500s
                # when the model mangles a long JSON argument — the
                # generation is discarded, so the turn never happened.
                # One nudged retry saves the run (5/118 eval cases
                # died to this); a second failure is a real error.
                log.warning("malformed tool-call JSON from %s — "
                            "retrying the turn once with a nudge", model)
                return await self._model_turn(
                    messages + [{"role": "user",
                                 "content": _TOOLCALL_JSON_NUDGE}],
                    tools_schema, model=model, think=think,
                    sampling=sampling, _retried_toolcall=True)
            raise RuntimeError(f"LiteLLM {r.status_code} for model "
                               f"'{model}': {body_txt}")
        data = r.json()
        # A degenerate/empty completion (or a misbehaving backend — e.g. a
        # brain that returned nothing) can come back with no choices or a
        # null message. Coerce to a safe empty assistant turn so the loop
        # ends the run cleanly instead of crashing on message.get(...).
        _choices = data.get("choices") or []
        _msg = (_choices[0].get("message") if _choices else None) \
            or {"role": "assistant", "content": None}
        out = {"message": _msg, "usage": data.get("usage", {}),
               "finish_reason": (_choices[0].get("finish_reason")
                                 if _choices else None)}
        # Server-parsed reasoning (llama.cpp reasoning_content) — keep a
        # bounded tail so the loop's completion-cap nudge can show the model
        # where its own chain-of-thought broke off instead of letting it
        # re-derive the whole chain on the retry (live: 2x full-cap turns).
        _rc = (_msg.get("reasoning_content") or "")
        if _rc:
            out["reasoning_tail"] = _rc[-1200:]
        return out

    async def complete(self, messages: list[dict], *, think: bool = False,
                       sampling: dict | None = None) -> dict:
        """One-shot, tool-free completion on the brain — for out-of-loop calls
        like /compact summarization. Returns {"content", "usage"}; any residual
        <think> block is stripped defensively (think is off, but a finetune can
        still emit one)."""
        r = await self._model_turn(messages, [], model=self.model, think=think,
                                   sampling=sampling)
        content = (r["message"].get("content") or "")
        content = re.sub(
            re.escape(_THINK_OPEN) + r".*?" + re.escape(_THINK_CLOSE),
            "", content, flags=re.S).strip()
        return {"content": content, "usage": r.get("usage") or {}}

    async def _model_turn_streaming(self, messages: list[dict],
                                    tools_schema: list[dict], on_token,
                                    model: str | None = None,
                                    think: bool = True,
                                    sampling: dict | None = None) -> dict:
        """Streaming model turn with the same malformed-tool-call-JSON rescue
        as _model_turn: llama.cpp 500s when a long argument loses its closing
        quote (almost always a multi-KB fs.write); the generation is discarded
        server-side, so one nudged retry is safe. A second failure is real.

        Transient transport failures (proxy restart, dropped connection, 5xx)
        are retried with backoff — but ONLY while nothing has been streamed
        to the UI yet: once tokens were delivered, a retry would duplicate
        visible output, so the error surfaces instead (readiness audit BE-3).
        """
        delays = self._retry_delays_s()
        attempt = 0
        emitted = False

        async def tracked_on_token(text, kind):
            nonlocal emitted
            emitted = True
            if on_token:
                await on_token(text, kind)

        while True:
            try:
                return await self._stream_with_toolcall_rescue(
                    messages, tools_schema, tracked_on_token,
                    model=model, think=think, sampling=sampling)
            except (httpx.HTTPError, RuntimeError) as e:
                if (emitted or not self._is_retryable_transport(e)
                        or attempt >= len(delays)):
                    raise
                log.warning("streamed model turn failed before any output "
                            "for %s (%s) — retrying in %gs",
                            model or self.model, e, delays[attempt])
                await asyncio.sleep(delays[attempt])
                attempt += 1

    async def _stream_with_toolcall_rescue(self, messages, tools_schema,
                                           on_token, model=None, think=True,
                                           sampling=None) -> dict:
        try:
            return await self._model_turn_stream(
                messages, tools_schema, on_token, model=model, think=think,
                sampling=sampling)
        except _ToolcallJson500:
            log.warning("malformed tool-call JSON from %s — retrying the "
                        "streamed turn once with a nudge", model or self.model)
            try:
                return await self._model_turn_stream(
                    messages + [{"role": "user",
                                 "content": _TOOLCALL_JSON_NUDGE}],
                    tools_schema, on_token, model=model, think=think,
                    sampling=sampling)
            except _ToolcallJson500:
                raise RuntimeError(
                    f"LiteLLM 500 for model '{model or self.model}': tool-call "
                    "arguments were not valid JSON twice in a row") from None

    async def _model_turn_stream(self, messages: list[dict],
                                 tools_schema: list[dict], on_token,
                                 model: str | None = None,
                                 think: bool = True,
                                 sampling: dict | None = None) -> dict:
        """Like _model_turn, but streams the response. Calls `await on_token(text)`
        for each content delta, assembles the streamed chunks back into the same
        {message, usage} shape the non-streaming path returns, and asks the proxy
        for usage via stream_options so cost still gets charged."""
        model = model or self.model
        body = _turn_body(model, messages, tools_schema, sampling, think,
                          stream=True, extra_local=self._local_aliases,
                          think_switch=self._think_aliases,
                          reasoning_budget=self._reasoning_budget)
        content_parts: list[str] = []     # answer text only (think stripped)
        tool_calls: dict[int, dict] = {}   # index -> assembled tool call
        usage: dict = {}
        finish_reason: str | None = None
        # Bounded tail of everything routed to the reasoning channel (server-
        # parsed reasoning_content AND inline <think> blocks) — the loop's
        # completion-cap nudge replays it so the model continues its chain
        # instead of re-deriving it from scratch on the retry turn.
        reasoning_tail = ""
        _raw_on_token = on_token

        async def on_token(text, kind):  # noqa: F811 — intentional wrap
            nonlocal reasoning_tail
            if kind == "reasoning" and text:
                reasoning_tail = (reasoning_tail + text)[-1200:]
            if _raw_on_token:
                await _raw_on_token(text, kind)

        # Streaming <think> splitter state. `pend` holds a trailing fragment that
        # might be the start of a split tag; `in_think` tracks which side we're on.
        pend = ""
        in_think = False

        async def consume(text: str):
            nonlocal pend, in_think
            pend += text
            while pend:
                if not in_think:
                    idx = pend.find(_THINK_OPEN)
                    if idx == -1:
                        keep = _suffix_prefix_len(pend, _THINK_OPEN)
                        emit = pend[:len(pend) - keep]
                        if emit:
                            content_parts.append(emit)
                            if on_token:
                                await on_token(emit, "brain")
                        pend = pend[len(pend) - keep:]
                        return
                    if idx > 0:
                        seg = pend[:idx]
                        content_parts.append(seg)
                        if on_token:
                            await on_token(seg, "brain")
                    pend = pend[idx + len(_THINK_OPEN):]
                    in_think = True
                else:
                    idx = pend.find(_THINK_CLOSE)
                    if idx == -1:
                        keep = _suffix_prefix_len(pend, _THINK_CLOSE)
                        emit = pend[:len(pend) - keep]
                        if emit and on_token:
                            await on_token(emit, "reasoning")
                        pend = pend[len(pend) - keep:]
                        return
                    if idx > 0 and on_token:
                        await on_token(pend[:idx], "reasoning")
                    pend = pend[idx + len(_THINK_CLOSE):]
                    in_think = False

        stall_s = self._stall_s()
        timeout_s = self._turn_timeout_s()
        guard = self._model_sem(model) or _NULL_ASYNC_CTX
        async with guard:
            try:
                async with self._http_client().stream(
                    "POST", f"{self.litellm_base}/v1/chat/completions", json=body,
                    headers=self._auth_headers(),
                    timeout=timeout_s or None,
                ) as r:
                    if r.status_code >= 400:
                        raw = await r.aread()
                        body_txt = raw.decode("utf-8", "replace")[:1000]
                        if _is_toolcall_json_500(r.status_code, body_txt):
                            raise _ToolcallJson500
                        log.error("streaming model turn failed: HTTP %s — %s",
                                  r.status_code, body_txt)
                        raise RuntimeError(f"LiteLLM {r.status_code} for model "
                                           f"'{model}': {body_txt}")
                    # Stall watchdog (zombie detector), bounding MODEL TURNS
                    # only — it can never fire during tool execution, where
                    # a long silent stretch (e.g. a code.delegate child) is
                    # legitimate. Two liveness rules:
                    #  1. absolute silence — no SSE line at all within
                    #     stall_s (the wait_for below): a wedged backend;
                    #  2. payload silence — lines keep arriving (proxy
                    #     keepalives, role-only/empty chunks) but no
                    #     content or tool-call delta for stall_s: alive on
                    #     the wire, not generating. Keepalives deliberately
                    #     do NOT count as liveness, or a zombie behind a
                    #     chatty proxy would only surface at timeout_s.
                    # The whole turn is additionally capped at timeout_s.
                    lines = r.aiter_lines()
                    turn_started = time.monotonic()
                    last_payload = turn_started
                    while True:
                        try:
                            if stall_s > 0:
                                line = await asyncio.wait_for(anext(lines), timeout=stall_s)
                            else:
                                line = await anext(lines)
                        except StopAsyncIteration:
                            break
                        except TimeoutError:
                            raise ModelTurnStalled(
                                f"model '{model}' produced no streamed output for "
                                f"{stall_s:g}s (budgets.stall_s) — treating the hung "
                                "turn as stalled; work so far is preserved") from None
                        now = time.monotonic()
                        if 0 < timeout_s < now - turn_started:
                            raise ModelTurnStalled(
                                f"model turn exceeded the {timeout_s:g}s total turn "
                                "timeout (orchestrator.turn_timeout_s); work so far "
                                "is preserved")
                        if stall_s > 0 and now - last_payload > stall_s:
                            raise ModelTurnStalled(
                                f"model '{model}' streamed no completion content for "
                                f"{stall_s:g}s (budgets.stall_s) — only keepalive/"
                                "empty traffic; treating the hung turn as stalled, "
                                "work so far is preserved") from None
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        if choices[0].get("finish_reason"):
                            finish_reason = choices[0]["finish_reason"]
                        delta = choices[0].get("delta") or {}
                        # Server-parsed chain-of-thought (llama.cpp splits the
                        # template-prefilled <think> block into reasoning_content;
                        # LiteLLM passes the field through). Route it to the UI's
                        # thinking view — and count it as liveness, or a long
                        # thinking stretch (empty content) would trip the stall
                        # watchdog's payload-silence rule.
                        rc = delta.get("reasoning_content")
                        if rc:
                            last_payload = now
                            if on_token:
                                await on_token(rc, "reasoning")
                        if delta.get("content"):
                            last_payload = now
                            await consume(delta["content"])
                        for tc in (delta.get("tool_calls") or []):
                            last_payload = now
                            i = tc.get("index", 0)
                            slot = tool_calls.setdefault(i, {
                                "id": None, "type": "function",
                                "function": {"name": "", "arguments": ""}})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["function"]["arguments"] += fn["arguments"]
            except httpx.ConnectTimeout:
                # Re-raise untranslated: the proxy is unreachable (restart
                # window), not a stalled live turn — _model_turn_streaming
                # retries it while nothing has been emitted yet.
                raise
            except httpx.TimeoutException:
                raise ModelTurnStalled(
                    f"model turn exceeded the {timeout_s:g}s total turn timeout "
                    "(orchestrator.turn_timeout_s); work so far is preserved") from None
            # Flush any held-back fragment (no further chunks to disambiguate it).
            if pend:
                if in_think:
                    if on_token:
                        await on_token(pend, "reasoning")
                else:
                    content_parts.append(pend)
                    if on_token:
                        await on_token(pend, "brain")
            message: dict = {"role": "assistant",
                             "content": "".join(content_parts).strip() or None}
            if tool_calls:
                message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
            out = {"message": message, "usage": usage,
                   "finish_reason": finish_reason}
            if reasoning_tail:
                out["reasoning_tail"] = reasoning_tail
            return out

    def _auth_headers(self) -> dict:
        """Authorization headers for the LiteLLM proxy. Empty when
        LITELLM_MASTER_KEY is unset — the shipped proxy config omits
        master_key then (runtime/cloud_store.render), which is fine for the
        default localhost-only bind. NOTE: a keyless proxy bound to anything
        but localhost is wide open — set a key before exposing it."""
        key = os.environ.get("LITELLM_MASTER_KEY")
        if not key:
            log.debug("LITELLM_MASTER_KEY unset — calling the proxy without "
                      "an Authorization header (keyless localhost mode)")
            return {}
        return {"Authorization": f"Bearer {key}"}
