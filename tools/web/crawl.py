"""web.crawl — extract the same structured data across a paginated set of pages.

Like web.extract, but walks pagination (next links, or ?page= / /page/N patterns)
up to a hard cap, extracting the described records from each page and appending
them to ONE merged JSON file. The running dataset lives in the file, not the
agent's context, so even a long crawl stays bounded — and the page cap is enforced
by the sub-agent's iteration budget, not merely requested. Only the file path and
a short report come back.
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult

_TOOLS = ["web.fetch", "web.render", "code.run", "fs.read", "fs.write"]
_MAX_PAGES_CEIL = 100


class WebCrawl(Tool):
    name = "web.crawl"
    read_only = True
    description = (
        "Crawl a paginated set of web pages and extract the same structured data "
        "from each into ONE merged JSON file. Give the START `url`, `describe` what "
        "to extract from every page, and a `max_pages` cap. It follows pagination — "
        "'next' links or ?page= / /page/N patterns — up to the cap, extracts the "
        "described records from each page, dedups, and appends them to the output "
        "file (so context stays bounded). Use it for 'all X across N pages'; for a "
        "single page use web.extract instead. Optional: `page_url` (a template with "
        "{page} for deterministic pagination, e.g. 'https://x.com/jobs?page={page}'), "
        "`start_page`, `schema`, `output`, `render`. Returns the file path and a "
        "short report — read the file for the data. It never invents records."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The first / start page."},
            "describe": {"type": "string",
                         "description": "What to extract from each page (fields + where), in plain language."},
            "max_pages": {"type": "integer",
                          "description": "Hard cap on pages to crawl (default 10)."},
            "page_url": {"type": "string",
                         "description": "Optional URL template with {page} for deterministic "
                                        "pagination, e.g. 'https://x.com/jobs?page={page}'."},
            "start_page": {"type": "integer",
                           "description": "First page number for page_url (default 1)."},
            "schema": {"type": "string",
                       "description": "Optional example JSON shape / field list to fix structure."},
            "output": {"type": "string",
                       "description": "Output filename (default 'crawled.json')."},
            "render": {"type": "boolean",
                       "description": "Force the headless browser (for JS-rendered pages)."},
        },
        "required": ["url", "describe"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        url = (args.get("url") or "").strip()
        describe = (args.get("describe") or "").strip()
        if not url or not describe:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="both 'url' and 'describe' are required")
        if getattr(ctx, "spawn", None) is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="web.crawl needs sub-agent spawning, unavailable here")
        try:
            max_pages = int(args.get("max_pages") or 10)
        except (TypeError, ValueError):
            max_pages = 10
        max_pages = max(1, min(max_pages, _MAX_PAGES_CEIL))
        output = (args.get("output") or "crawled.json").strip()
        schema = (args.get("schema") or "").strip()
        page_url = (args.get("page_url") or "").strip()
        try:
            start_page = int(args.get("start_page") or 1)
        except (TypeError, ValueError):
            start_page = 1

        fetch_step = ("Load each page with web.render (headless browser)." if args.get("render")
                      else "Fetch each page with web.fetch; fall back to web.render if a page is "
                           "JS-heavy or returns little usable content.")
        schema_step = (f"Match this shape: {schema}" if schema else
                       "Use an array of objects; short snake_case field names.")
        if page_url:
            pagination = (f"Pages follow this URL template: {page_url} — substitute {{page}} "
                          f"with each number from {start_page} to {start_page + max_pages - 1}. "
                          "Stop early if a page 404s or yields no records.")
        else:
            pagination = ("Find the next page from a 'next' / '\u203a' link or a ?page= / /page/N "
                          "URL pattern, staying on the SAME site. If the first page reveals a "
                          "page-number pattern, use it directly.")

        task = (
            "Crawl a paginated set of pages and extract structured data from each into "
            "ONE merged JSON file.\n\n"
            f"START URL: {url}\n"
            f"EXTRACT from every page: {describe}\n"
            f"TARGET SHAPE: {schema_step}\n"
            f"MAX PAGES: {max_pages}  (a hard cap — never exceed it)\n\n"
            f"PAGINATION: {pagination}\n\n"
            "For EACH page, in order:\n"
            f"  a. {fetch_step}\n"
            "  b. Extract the described records. Use null for a genuinely missing field; "
            "NEVER invent, guess, or fill in values that aren't on the page.\n"
            f"  c. APPEND to `{output}`: read the current JSON array with fs.read (start from "
            "[] if the file doesn't exist), add the new records, write it back with fs.write. "
            "Keep the running data in the FILE, not in your context — do not hold all pages at "
            "once.\n"
            "  d. Dedup: skip records already saved (match on the most identifying field).\n"
            "Stop when you have done MAX PAGES pages, there is no next page, or a page yields no "
            "new records.\n"
            "Finally, validate the file parses (json.loads via code.run language=python).\n"
            "Report: pages crawled, total records saved, the fields captured, and any pages that "
            "failed or looked different. Keep it short — do NOT paste the dataset.\n"
        )

        # Hard backstop: bound the crawl by iteration budget (~4 steps/page + slack),
        # so the cap holds even if the agent miscounts. Clamped to the parent budget.
        child = await ctx.spawn(task, tools=_TOOLS, name="crawler",
                                budget={"max_iterations": max_pages * 4 + 8})
        status = child.get("status")
        return ToolResult(
            status="ok" if status in ("ok", None, "budget_exceeded") else "error",
            tool_name=self.name,
            result={
                "saved_to": output,
                "max_pages": max_pages,
                "hit_budget": status == "budget_exceeded",
                "report": child.get("answer"),
                "files_changed": child.get("files_changed") or [],
                "sub_run_id": child.get("run_id"),
            },
            error=None if status in ("ok", None, "budget_exceeded")
            else f"crawl sub-agent status: {status}")
