"""web.request — generic HTTP for REST APIs and webhooks.

web.fetch is deliberately narrow (GET, returns stripped text). This is the
escape hatch for real APIs: any method, custom headers, JSON or raw body, raw
response. The posture matches the rest of the web namespace:

- SSRF-guarded like web.fetch: loopback/link-local/metadata targets refused,
  hostnames resolved and checked, redirect hops re-validated (services on this
  box have dedicated tools; the LiteLLM admin API on :4000 must not be
  reachable from a model-driven call)
- read methods (GET/HEAD/OPTIONS) run ungated like web.fetch; write methods
  (POST/PUT/PATCH/DELETE) pause for human approval — they change remote state
- response and request bodies are byte-capped; marked private so API responses
  (which may carry account data) aren't forwarded to cloud LLMs by default
"""

from __future__ import annotations

import json as jsonlib
from urllib.parse import urljoin, urlparse

import httpx

from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.web.search_fetch import (
    _MAX_REDIRECTS, _UA, SsrfRefused, refusal_text, ssrf_refusal)

_MAX_WIRE_BYTES = 8 * 1024 * 1024     # response read cap (same as web.fetch)
_MAX_BODY_CHARS = 1_000_000           # outgoing body cap
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
_METHODS = _READ_METHODS | {"POST", "PUT", "PATCH", "DELETE"}


class WebRequest(Tool):
    name = "web.request"
    description = (
        "Make an HTTP request to an API endpoint and get the raw response: any "
        "method (GET/POST/PUT/PATCH/DELETE), custom headers, optional JSON or raw "
        "body. Use for REST APIs and webhooks that web.fetch (GET, text-only) "
        "can't reach. Write methods ask for confirmation. Loopback targets are "
        "refused — use ops.run for services on this box. Never put private local "
        "data into a remote request without the user's say-so."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL (with https://)."},
            "method": {"type": "string",
                       "enum": sorted(_METHODS),
                       "default": "GET"},
            "headers": {"type": "object",
                        "description": "Request headers, e.g. {'Authorization': 'Bearer …'}."},
            "json": {"type": "object",
                     "description": "Send this object as a JSON body (sets content-type)."},
            "body": {"type": "string",
                     "description": "Raw request body (alternative to json)."},
            "timeout_s": {"type": "integer", "default": 30, "minimum": 1, "maximum": 120},
            "max_chars": {"type": "integer", "default": 20000, "minimum": 500,
                          "maximum": 100000,
                          "description": "Response body cap returned to you."},
        },
        "required": ["url"],
    }

    def needs_confirmation(self, args: dict, ctx: ToolContext) -> bool:
        # Writes change remote state; reads are as harmless as web.fetch.
        return (args.get("method") or "GET").upper() not in _READ_METHODS

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        url = (args.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"unsupported scheme: {parsed.scheme!r}")
        reason = await ssrf_refusal(parsed.hostname or "")
        if reason:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=refusal_text("web.request", reason, parsed.hostname))
        method = (args.get("method") or "GET").upper()
        if method not in _METHODS:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"unsupported method {method!r} "
                                    f"(one of {', '.join(sorted(_METHODS))})")

        headers = {"User-Agent": _UA}
        for k, v in (args.get("headers") or {}).items():
            headers[str(k)] = str(v)

        kwargs: dict = {"headers": headers}
        if args.get("json") is not None:
            kwargs["json"] = args["json"]
        elif args.get("body") is not None:
            body = str(args["body"])
            if len(body) > _MAX_BODY_CHARS:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"body exceeds the {_MAX_BODY_CHARS}-char cap")
            kwargs["content"] = body
        if method in _READ_METHODS and ("json" in kwargs or "content" in kwargs):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"{method} with a body is refused — use POST/PUT/PATCH")

        timeout = min(int(args.get("timeout_s", 30)), 120)
        max_chars = int(args.get("max_chars", 20000))
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                # Manual redirect following: re-check every hop against the
                # SSRF guard so a public URL can't 302 into loopback/metadata.
                for _ in range(_MAX_REDIRECTS + 1):
                    hop_host = urlparse(url).hostname or ""
                    hop_reason = await ssrf_refusal(hop_host)
                    if hop_reason:
                        raise SsrfRefused(refusal_text("web.request", hop_reason, hop_host))
                    async with client.stream(method, url, **kwargs) as r:
                        loc = (r.headers.get("location", "")
                               if getattr(r, "is_redirect", False) else "")
                        if loc:
                            url = urljoin(url, loc)
                            continue
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in r.aiter_bytes():
                            size += len(chunk)
                            if size > _MAX_WIRE_BYTES:
                                break
                            chunks.append(chunk)
                        status = r.status_code
                        resp_headers = dict(r.headers)
                    break
                else:
                    raise RuntimeError(f"too many redirects (>{_MAX_REDIRECTS})")
        except Exception as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"{type(e).__name__}: {e}")

        text = b"".join(chunks).decode("utf-8", "replace")
        ctype = resp_headers.get("content-type", "")
        result = {
            "url": url, "method": method, "status_code": status,
            "content_type": ctype,
            "truncated": size > _MAX_WIRE_BYTES or len(text) > max_chars,
        }
        if "application/json" in ctype or "text/json" in ctype:
            try:
                # Parse the full (wire-capped) body; the ToolResult envelope
                # applies its own 20k-char cap when serializing for the model.
                result["json"] = jsonlib.loads(text)
            except (ValueError, TypeError):
                result["body"] = text[:max_chars]
        else:
            result["body"] = text[:max_chars]
        return ToolResult(status="ok", result=result, tool_name=self.name)
