"""web.request — generic HTTP for REST APIs and webhooks.

web.fetch is deliberately narrow (GET, returns stripped text). This is the
escape hatch for real APIs: any method, custom headers, JSON or raw body, raw
response. The posture matches the rest of the web namespace:

- SSRF-guarded like web.fetch: loopback/link-local/metadata targets refused,
  hostnames resolved and checked, redirect hops re-validated (services on this
  box have dedicated tools; the LiteLLM admin API on :4000 must not be
  reachable from a model-driven call). A redirect that changes origin drops
  Authorization/Cookie instead of replaying them to the new host
- read methods (GET/HEAD/OPTIONS) run ungated like web.fetch; write methods
  (POST/PUT/PATCH/DELETE) pause for human approval — they change remote state
- response and request bodies are byte-capped; marked private so API responses
  (which may carry account data) aren't forwarded to cloud LLMs by default
- a 429 rate limit is retried once internally after the server's Retry-After
  hint (capped at 8s); still limited → hard error, so the loop's failure
  tracking sees it and the model backs off instead of re-issuing forever
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from urllib.parse import urljoin, urlparse

import httpx

from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.web.search_fetch import _MAX_REDIRECTS, _UA, SsrfRefused, refusal_text, ssrf_refusal

_MAX_WIRE_BYTES = 8 * 1024 * 1024     # response read cap (same as web.fetch)
_MAX_BODY_CHARS = 1_000_000           # outgoing body cap
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
_METHODS = _READ_METHODS | {"POST", "PUT", "PATCH", "DELETE"}
_CREDENTIAL_HEADERS = {"authorization", "cookie"}


def _retry_after_s(headers: dict) -> float:
    """Retry-After in seconds (the HTTP-date form is deliberately unsupported
    — APIs that rate-limit send seconds). Capped so a hostile/buggy server
    can't park a run; absent or unparseable → a short default wait."""
    try:
        return max(0.0, min(float(headers.get("retry-after", "") or 3.0), 8.0))
    except (TypeError, ValueError):
        return 3.0


def _origin(url: str) -> tuple:
    """(scheme, host, port) with default ports filled — redirects that change
    any of these must not carry the caller's credentials to the new origin."""
    p = urlparse(url)
    return (p.scheme, (p.hostname or "").lower(),
            p.port or (443 if p.scheme == "https" else 80))


class WebRequest(Tool):
    name = "web.request"
    description = (
        "Make an HTTP request to an API endpoint and get the raw response: any "
        "method (GET/POST/PUT/PATCH/DELETE), custom headers, optional JSON or raw "
        "body. Use for REST APIs and webhooks that web.fetch (GET, text-only) "
        "can't reach. Write methods ask for confirmation. Loopback targets are "
        "refused — use ops.run for services on this box. A 429 rate limit is "
        "retried once automatically, then comes back as an error — back off or "
        "switch sources, never re-issue it in a loop. Never put private local "
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
            "offset": {"type": "integer", "default": 0, "minimum": 0,
                       "description": "Non-JSON bodies: start reading at this char "
                                      "position (from a previous call's truncation "
                                      "hint) to page through long responses."},
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
        offset = max(0, int(args.get("offset", 0) or 0))
        orig_url, base_headers = url, dict(headers)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                # A 429 is transient by nature: one internal retry after the
                # server's Retry-After hint (capped). Beyond that it becomes
                # a hard error below — tool-level ok results with a 429
                # payload are invisible to the loop's failure tracking, and
                # the model re-issues the identical call forever (live:
                # gaia-46719c30, five identical Semantic Scholar 429s).
                for attempt in range(2):
                    url, headers = orig_url, dict(base_headers)
                    kwargs["headers"] = headers
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
                                next_url = urljoin(url, loc)
                                if _origin(next_url) != _origin(url):
                                    # Cross-origin hop: never replay credentials —
                                    # a bearer token for site A must not leak to
                                    # whatever host A redirects to.
                                    headers = {k: v for k, v in headers.items()
                                               if k.lower() not in _CREDENTIAL_HEADERS}
                                    kwargs["headers"] = headers
                                url = next_url
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
                    if status != 429 or attempt:
                        break
                    await asyncio.sleep(_retry_after_s(resp_headers))
        except Exception as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"{type(e).__name__}: {e}")

        if status == 429:
            return ToolResult(
                status="error", result=None, tool_name=self.name,
                error=f"HTTP 429 rate limited by "
                      f"{urlparse(orig_url).hostname} — still limited after "
                      "an automatic retry with the server's Retry-After "
                      "wait. Do NOT re-issue the same call right away: it "
                      "will keep failing. Wait a minute, or get the same "
                      "data from a different source/endpoint.")

        text = b"".join(chunks).decode("utf-8", "replace")
        ctype = resp_headers.get("content-type", "")
        result = {
            "url": url, "method": method, "status_code": status,
            "content_type": ctype,
            "truncated": size > _MAX_WIRE_BYTES or len(text) > offset + max_chars,
        }
        if "application/json" in ctype or "text/json" in ctype:
            try:
                # Parse the full (wire-capped) body; the ToolResult envelope
                # applies its own 20k-char cap when serializing for the model.
                result["json"] = jsonlib.loads(text)
            except (ValueError, TypeError):
                result["body"] = text[offset:offset + max_chars]
        else:
            result["body"] = text[offset:offset + max_chars]
        # Pagination hint on text bodies (JSON parses whole): without an offset
        # to page with, the model refetches the same truncated head in a loop.
        # Only when the char cap is the binding constraint — a wire-capped body
        # (>8MB) can't be paged further by refetching.
        if "body" in result and len(text) > offset + max_chars:
            end = offset + len(result["body"])
            result["offset"] = offset
            result["original_length"] = len(text)
            result["hint"] = (f"showing chars {offset}–{end} of {len(text)} — "
                              f"call again with offset={end} to continue reading")
        return ToolResult(status="ok", result=result, tool_name=self.name)
