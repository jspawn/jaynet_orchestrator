"""Web search and fetch tools.

Search backend priority (local first, cloud if necessary):
  1. Self-hosted SearxNG (if tools.web.search_endpoint configured) — free, local.
  2. Tavily API (if TAVILY_API_KEY is set) — LLM-oriented search with extracted
     snippets. Paid external API; leaves your network.
  3. DuckDuckGo HTML (always available) — free fallback, no key.

Fetch backend priority:
  1. Tavily /extract (if TAVILY_API_KEY set) — clean content extraction.
  2. Direct httpx GET + tag-strip (always available) — free fallback.

The fallback chain means the tools work with no API key at all; Tavily simply
upgrades quality when its key is present. Config lives under tools.web in
runtime.yaml; the key is read from the TAVILY_API_KEY env var.
"""

from __future__ import annotations

import ipaddress
import os
import re
import html as html_lib
from urllib.parse import urlparse

import httpx

from runtime.tool_base import Tool, ToolContext, ToolResult


_DDG_URL = "https://html.duckduckgo.com/html/"
_TAVILY_SEARCH = "https://api.tavily.com/search"
_TAVILY_EXTRACT = "https://api.tavily.com/extract"
_UA = "Mozilla/5.0 (X11; Linux x86_64) Orchestrator/1.0"
# Hard cap on a direct-fetch response body: read at most this many bytes off the
# wire, never slurp an unbounded page into memory (the char cap applies after).
_MAX_FETCH_BYTES = 8 * 1024 * 1024


def _is_loopback_host(host: str) -> bool:
    """True for loopback/unspecified targets (127.0.0.0/8, ::1, 0.0.0.0, ::,
    localhost) — the box's own admin surfaces (e.g. the LiteLLM admin API on
    :4000), which have dedicated local tools. RFC1918 LAN hosts are NOT
    loopback: the operator fetches LAN services legitimately."""
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def _tavily_key() -> str | None:
    key = os.environ.get("TAVILY_API_KEY")
    return key or None


def html_to_text(body: str) -> str:
    """Strip <script>/<style> and tags from HTML, returning collapsed plain text.
    Shared by web.fetch (direct GET) and web.render (post-JS DOM)."""
    body = re.sub(r"<script\b[^>]*>.*?</script>", " ", body,
                  flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"<style\b[^>]*>.*?</style>", " ", body,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", body)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()

class WebSearch(Tool):
    name = "web.search"
    read_only = True
    description = (
        "Search the web for current information. Returns a list of "
        "{title, url, snippet} results. Use for facts that may have changed, "
        "recent events, or anything requiring up-to-date sources."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query, 1-6 words ideal."},
            "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        query = args["query"]
        n = min(int(args.get("max_results", 5)), 10)
        cfg = ctx.config.get("tools", {}).get("web", {})
        endpoint = cfg.get("search_endpoint")

        # Priority: SearxNG -> Tavily -> DDG. Each falls through on failure so a
        # transient outage in one backend degrades instead of erroring the run.
        errors = []
        if endpoint:
            try:
                return ToolResult(status="ok",
                                  result=await self._search_searxng(endpoint, query, n))
            except Exception as e:
                errors.append(f"searxng: {type(e).__name__}: {e}")

        if _tavily_key() and cfg.get("tavily_enabled", True):
            try:
                return ToolResult(status="ok",
                                  result=await self._search_tavily(query, n, cfg))
            except Exception as e:
                errors.append(f"tavily: {type(e).__name__}: {e}")

        try:
            return ToolResult(status="ok", result=await self._search_ddg(query, n))
        except Exception as e:
            errors.append(f"ddg: {type(e).__name__}: {e}")
            return ToolResult(status="error", result=None,
                              error="all search backends failed (" + "; ".join(errors) + ")")

    async def _search_tavily(self, query: str, n: int, cfg: dict) -> list[dict]:
        body = {
            "query": query,
            "max_results": n,
            "search_depth": cfg.get("tavily_depth", "basic"),  # "basic" | "advanced"
            "include_answer": False,
        }
        headers = {"Authorization": f"Bearer {_tavily_key()}"}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(_TAVILY_SEARCH, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
        out = []
        for item in (data.get("results") or [])[:n]:
            out.append({
                "title": (item.get("title") or "")[:200],
                "url": item.get("url", ""),
                "snippet": (item.get("content") or "")[:300],
            })
        return out

    async def _search_searxng(self, endpoint: str, query: str, n: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": _UA}) as client:
            r = await client.get(endpoint, params={"q": query, "format": "json"})
            r.raise_for_status()
            data = r.json()
        out = []
        for item in (data.get("results") or [])[:n]:
            out.append({
                "title": item.get("title", "")[:200],
                "url": item.get("url", ""),
                "snippet": (item.get("content") or "")[:300],
            })
        return out

    async def _search_ddg(self, query: str, n: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": _UA},
                                     follow_redirects=True) as client:
            r = await client.post(_DDG_URL, data={"q": query}, timeout=15)
            r.raise_for_status()
            body = r.text
        out: list[dict] = []
        pattern = re.compile(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        for m in pattern.finditer(body):
            if len(out) >= n:
                break
            url = html_lib.unescape(m.group(1))
            if url.startswith("//duckduckgo.com/l/?uddg="):
                from urllib.parse import unquote, parse_qs
                qs = parse_qs(url.split("?", 1)[1])
                url = unquote(qs.get("uddg", [""])[0])
            title = re.sub(r"<[^>]+>", "", m.group(2))
            snippet = re.sub(r"<[^>]+>", "", m.group(3))
            out.append({
                "title": html_lib.unescape(title).strip()[:200],
                "url": url,
                "snippet": html_lib.unescape(snippet).strip()[:300],
            })
        return out


class WebFetch(Tool):
    name = "web.fetch"
    read_only = True
    description = (
        "Fetch the text content of a URL. Returns plain-text extracted from HTML, "
        "truncated to a reasonable length. Use after web.search to read a specific page."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL (with https://)."},
            "max_chars": {"type": "integer", "default": 20000, "minimum": 500, "maximum": 100000},
        },
        "required": ["url"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        url = args["url"]
        max_chars = int(args.get("max_chars", 20000))
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ToolResult(status="error", result=None,
                              error=f"unsupported scheme: {parsed.scheme}")
        if _is_loopback_host(parsed.hostname or ""):
            return ToolResult(status="error", result=None,
                              error=(f"web.fetch refuses loopback targets ('{parsed.hostname}') "
                                     "— use the dedicated local tools (e.g. ops.run) for "
                                     "services on this box"))

        cfg = ctx.config.get("tools", {}).get("web", {})
        timeout = cfg.get("fetch_timeout_s", 15)
        cap = min(max_chars, cfg.get("max_content_chars", 50000))

        # Tavily /extract first (clean text), fall back to direct GET + strip.
        if _tavily_key() and cfg.get("tavily_enabled", True):
            try:
                text = await self._fetch_tavily(url)
                truncated = len(text) > cap
                return ToolResult(status="ok", result={
                    "url": url, "content": text[:cap],
                    "truncated": truncated, "original_length": len(text),
                    "via": "tavily",
                })
            except Exception:
                pass  # fall through to direct fetch

        try:
            text = await self._fetch_direct(url, timeout)
        except Exception as e:
            return ToolResult(status="error", result=None,
                              error=f"fetch failed: {type(e).__name__}: {e}")
        truncated = len(text) > cap
        return ToolResult(status="ok", result={
            "url": url, "content": text[:cap],
            "truncated": truncated, "original_length": len(text),
            "via": "direct",
        })

    async def _fetch_tavily(self, url: str) -> str:
        headers = {"Authorization": f"Bearer {_tavily_key()}"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(_TAVILY_EXTRACT, json={"urls": [url]}, headers=headers)
            r.raise_for_status()
            data = r.json()
        results = data.get("results") or []
        if not results:
            raise RuntimeError("tavily extract returned no content")
        # Tavily returns raw_content per URL.
        return results[0].get("raw_content") or ""

    async def _fetch_direct(self, url: str, timeout: int) -> str:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": _UA},
                                     follow_redirects=True) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                # Stop reading past _MAX_FETCH_BYTES — a huge (or endless) body
                # is never slurped fully into memory.
                chunks: list[bytes] = []
                size = 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_FETCH_BYTES:
                        break
                    chunks.append(chunk)
        return html_to_text(b"".join(chunks).decode("utf-8", "replace"))
