"""web.extract — pull structured data off a web page into a JSON file.

Give a URL and a plain-language description of what to extract and where it sits
on the page; this runs the extraction in an isolated sub-agent (so the page's raw
content never bloats the caller's context) that fetches the page — rendering
JS-heavy sites when a plain fetch comes back thin — locates the described data,
builds JSON, validates that it parses, and saves it to a file. Only the file path
and a short report come back. It extracts what's actually on the page; it does not
invent values.
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult

_TOOLS = ["web.fetch", "web.render", "browser.screenshot", "code.execute", "fs.write"]


class WebExtract(Tool):
    name = "web.extract"
    description = (
        "Extract structured data from a web page into a JSON file. Give the `url` "
        "and `describe` what data to pull and where it is on the page (e.g. 'the "
        "pricing tiers in the table under Plans — name, monthly price, and included "
        "features', or 'every job listing's title, company, location, and posted "
        "date'). It fetches the page (rendering JS-heavy sites automatically), "
        "extracts the described data as JSON, validates that it parses, and saves it "
        "to a file to work with. Optionally pass `schema` (an example shape or field "
        "list) to fix the structure, `output` for the filename, and `render:true` to "
        "force the headless browser. Returns the file path and a short report — read "
        "the file for the data. It never invents values not present on the page."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The page to extract from."},
            "describe": {"type": "string",
                         "description": "What data to extract and where it is on the page — "
                                        "the fields and the section, in plain language."},
            "schema": {"type": "string",
                       "description": "Optional example JSON shape or field list to fix the "
                                      "structure, e.g. '[{name, price, features[]}]'."},
            "output": {"type": "string",
                       "description": "Output filename (default 'extracted.json')."},
            "render": {"type": "boolean",
                       "description": "Force the headless browser (for heavily JS-rendered pages)."},
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
                              error="web.extract needs sub-agent spawning, unavailable here")
        output = (args.get("output") or "extracted.json").strip()
        schema = (args.get("schema") or "").strip()
        fetch_step = ("Use web.render (headless browser) to load the page — it is "
                      "JS-heavy." if args.get("render") else
                      "Fetch it with web.fetch. If the page is JS-heavy or web.fetch "
                      "returns little usable content, fall back to web.render.")
        schema_step = (f"Match this structure:\n{schema}\n" if schema else
                       "Use an array of objects for repeated records; pick short, "
                       "snake_case field names.\n")

        task = (
            "Extract structured data from a web page and save it as JSON.\n\n"
            f"URL: {url}\n"
            f"EXTRACT: {describe}\n\n"
            f"TARGET SHAPE: {schema_step}\n"
            "Steps:\n"
            f"1. {fetch_step}\n"
            "2. Locate exactly the data described. Ignore navigation, ads, cookie "
            "banners, and unrelated boilerplate.\n"
            "3. Build the JSON. Use null for a field that's genuinely missing; do "
            "NOT invent, guess, or fill in values that aren't on the page.\n"
            "4. Validate it parses: run json.loads on the JSON string via "
            "code.execute. If it fails, fix it and re-validate.\n"
            f"5. Save the validated JSON to `{output}` with fs.write.\n"
            "6. Report back: the file path, how many records you extracted, the "
            "fields captured, and anything that was missing, ambiguous, or that you "
            "had to skip. Keep the report short — do NOT paste the whole dataset.\n"
        )
        child = await ctx.spawn(task, tools=_TOOLS, name="extractor")
        status = child.get("status")
        return ToolResult(
            status="ok" if status in ("ok", None) else "error",
            tool_name=self.name,
            result={
                "saved_to": output,
                "report": child.get("answer"),
                "files_changed": child.get("files_changed") or [],
                "sub_run_id": child.get("run_id"),
            },
            error=None if status in ("ok", None) else f"extraction sub-agent status: {status}")
