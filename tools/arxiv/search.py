"""arXiv tools — structured paper search for AI/ML research.

Distinct from web.search: hits the arXiv Atom API directly and returns clean,
structured metadata (id, title, authors, abstract, categories, pdf url) so the
agent can survey the literature and pull the right papers — e.g. when building
a world model and you want the Dreamer / JEPA / Genie lineage.

No API key needed. arXiv asks for <= 1 request / 3s; the agent rarely bursts,
but tools.arxiv.min_interval_s can enforce a courtesy delay if you like.

NOTE: the live HTTP call could not be exercised in the build sandbox (arXiv is
off-net there); the Atom parser was tested against a representative payload.
"""

from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET

import httpx

from runtime.tool_base import Tool, ToolContext, ToolResult

_API = "https://export.arxiv.org/api/query"
_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}

_last_call = [0.0]   # module-level courtesy throttle


def _cfg(ctx: ToolContext) -> dict:
    return ctx.config.get("tools", {}).get("arxiv", {})


def _short_id(raw_id: str) -> str:
    # <id> looks like http://arxiv.org/abs/2301.12345v2
    return raw_id.rsplit("/abs/", 1)[-1] if "/abs/" in raw_id else raw_id


def _parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    papers = []
    for e in root.findall("a:entry", _NS):
        def text(tag):
            node = e.find(tag, _NS)
            return node.text.strip() if node is not None and node.text else ""

        authors = [a.findtext("a:name", default="", namespaces=_NS).strip()
                   for a in e.findall("a:author", _NS)]
        # links: alternate = abs page, title="pdf" = pdf
        pdf_url, abs_url = "", ""
        for ln in e.findall("a:link", _NS):
            if ln.get("title") == "pdf":
                pdf_url = ln.get("href", "")
            elif ln.get("rel") == "alternate":
                abs_url = ln.get("href", "")
        prim = e.find("arxiv:primary_category", _NS)
        cats = [c.get("term") for c in e.findall("a:category", _NS) if c.get("term")]

        papers.append({
            "id": _short_id(text("a:id")),
            "title": " ".join(text("a:title").split()),
            "authors": [a for a in authors if a],
            "published": text("a:published")[:10],
            "updated": text("a:updated")[:10],
            "summary": " ".join(text("a:summary").split()),
            "primary_category": prim.get("term") if prim is not None else (cats[0] if cats else ""),
            "categories": cats,
            "comment": text("arxiv:comment"),
            "doi": text("arxiv:doi"),
            "pdf_url": pdf_url,
            "abs_url": abs_url,
        })
    return papers


async def _fetch(params: dict, ctx: ToolContext) -> str:
    interval = float(_cfg(ctx).get("min_interval_s", 0) or 0)
    if interval:
        wait = interval - (time.monotonic() - _last_call[0])
        if wait > 0:
            await asyncio.sleep(wait)
    _last_call[0] = time.monotonic()
    url = _cfg(ctx).get("api_url", _API)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.text


class ArxivSearch(Tool):
    name = "arxiv.search"
    read_only = True
    description = (
        "Search arXiv for papers. Returns structured metadata (id, title, authors, "
        "abstract, categories, pdf url). Use for literature surveys and finding "
        "specific work. Query supports field prefixes: ti: (title), au: (author), "
        "abs: (abstract), cat: (category, e.g. cs.LG). Combine with AND/OR."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "e.g. 'world model reinforcement learning' or "
                                     "'ti:JEPA' or 'cat:cs.LG AND abs:diffusion'."},
            "max_results": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            "sort_by": {"type": "string",
                        "enum": ["relevance", "lastUpdatedDate", "submittedDate"],
                        "default": "relevance"},
            "sort_order": {"type": "string", "enum": ["ascending", "descending"],
                           "default": "descending"},
        },
        "required": ["query"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        params = {
            "search_query": args["query"],
            "start": 0,
            "max_results": min(int(args.get("max_results", 10)), 50),
            "sortBy": args.get("sort_by", "relevance"),
            "sortOrder": args.get("sort_order", "descending"),
        }
        try:
            xml_text = await _fetch(params, ctx)
            papers = _parse_feed(xml_text)
        except httpx.HTTPStatusError as e:
            return ToolResult(status="error", result=None,
                              error=f"HTTP {e.response.status_code}: {e.response.text[:300]}")
        except Exception as e:
            return ToolResult(status="error", result=None, error=f"{type(e).__name__}: {e}")
        # Trim abstracts so a 50-result survey doesn't blow the context window.
        for p in papers:
            if len(p["summary"]) > 500:
                p["summary"] = p["summary"][:500] + "…"
        return ToolResult(status="ok", result={"query": args["query"],
                                                "count": len(papers), "papers": papers})


class ArxivGet(Tool):
    name = "arxiv.get"
    read_only = True
    description = ("Fetch full metadata and the complete abstract for one or more "
                  "arXiv ids (e.g. '2301.12345' or '2301.12345v2'). Use after "
                  "arxiv.search to read a paper's full abstract.")
    parameters = {
        "type": "object",
        "properties": {
            "ids": {"type": "array", "items": {"type": "string"},
                    "description": "arXiv ids, with or without version suffix."},
        },
        "required": ["ids"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        ids = args["ids"]
        if not ids:
            return ToolResult(status="error", result=None, error="no ids given")
        params = {"id_list": ",".join(ids), "max_results": len(ids)}
        try:
            xml_text = await _fetch(params, ctx)
            papers = _parse_feed(xml_text)
        except httpx.HTTPStatusError as e:
            return ToolResult(status="error", result=None,
                              error=f"HTTP {e.response.status_code}: {e.response.text[:300]}")
        except Exception as e:
            return ToolResult(status="error", result=None, error=f"{type(e).__name__}: {e}")
        return ToolResult(status="ok", result={"count": len(papers), "papers": papers})
