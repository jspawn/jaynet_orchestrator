"""Model-call plumbing for the agent loop.

Everything that talks to LiteLLM: request-body construction (sampler params,
the local-only jinja thinking switch), the shared keep-alive httpx client,
per-backend concurrency gates, and the two model-turn paths (buffered and
streaming) with their stall/timeout watchdogs.

Split out of runtime/loop.py — AgentRuntime composes this via ModelClientMixin,
so the host class must provide: self.config, self.model, self.litellm_base,
self._local_concurrency, self._model_sems, self._local_aliases.
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
               extra_local: frozenset = frozenset()) -> dict:
    """Build the /v1/chat/completions body shared by both model-turn paths.

    `chat_template_kwargs` (the llama.cpp jinja thinking switch) is added ONLY for
    local models; cloud sub-agents run at the provider's default thinking mode
    (any reasoning is stripped from the answer downstream).
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
        body["chat_template_kwargs"] = {"enable_thinking": think}
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


class ModelClientMixin:
    """The LiteLLM-facing half of AgentRuntime (see module docstring for the
    attributes the host class must provide)."""

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

    async def _model_turn(self, messages: list[dict], tools_schema: list[dict],
                          model: str | None = None, think: bool = True,
                          sampling: dict | None = None) -> dict:
        """One call to a model via LiteLLM (local brain or a cloud sub-agent)."""
        model = model or self.model
        body = _turn_body(model, messages, tools_schema, sampling, think,
                          stream=False, extra_local=self._local_aliases)
        timeout_s = self._turn_timeout_s()
        guard = self._model_sem(model) or _NULL_ASYNC_CTX
        try:
            async with guard:
                r = await self._http_client().post(
                    f"{self.litellm_base}/v1/chat/completions",
                    json=body,
                    headers=self._auth_headers(),
                    timeout=timeout_s or None,
                )
                if r.status_code >= 400:
                    # Surface the proxy's actual explanation instead of a bare code.
                    body = r.text[:1000]
                    log.error("model turn failed: HTTP %s from %s — %s",
                              r.status_code, model, body)
                    raise RuntimeError(f"LiteLLM {r.status_code} for model "
                                       f"'{model}': {body}")
                data = r.json()
                # A degenerate/empty completion (or a misbehaving backend — e.g. a
                # brain that returned nothing) can come back with no choices or a
                # null message. Coerce to a safe empty assistant turn so the loop
                # ends the run cleanly instead of crashing on message.get(...).
                _choices = data.get("choices") or []
                _msg = (_choices[0].get("message") if _choices else None) \
                    or {"role": "assistant", "content": None}
                return {"message": _msg, "usage": data.get("usage", {})}
        except httpx.TimeoutException:
            # No token heartbeat exists on this path — the total turn timeout is
            # its only liveness bound, so expiry means "stalled", not an error.
            raise ModelTurnStalled(
                f"model turn exceeded the {timeout_s:g}s total turn timeout "
                "(orchestrator.turn_timeout_s); ending the run with work so far "
                "preserved") from None

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
        """Like _model_turn, but streams the response. Calls `await on_token(text)`
        for each content delta, assembles the streamed chunks back into the same
        {message, usage} shape the non-streaming path returns, and asks the proxy
        for usage via stream_options so cost still gets charged."""
        model = model or self.model
        body = _turn_body(model, messages, tools_schema, sampling, think,
                          stream=True, extra_local=self._local_aliases)
        content_parts: list[str] = []     # answer text only (think stripped)
        tool_calls: dict[int, dict] = {}   # index -> assembled tool call
        usage: dict = {}
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
                        except asyncio.TimeoutError:
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
            return {"message": message, "usage": usage}

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
