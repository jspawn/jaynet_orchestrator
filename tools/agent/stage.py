"""context.stage — move oversized text OUT of the conversation into a file.

The bias against hauling bulk through the context window: when a tool result
(or a draft) is too big to keep quoting, stage it into the workspace and get
back a path. From then on the file is addressed PROGRAMMATICALLY — sliced with
code.execute (optionally mapping llm_query over the slices), read in ranges
with fs.read — instead of re-entering the context whole. This is the
"context-as-variable" move of the RLM pattern made a one-call habit.

Same-content staging is idempotent: the filename carries a content hash, so
re-staging identical text returns the existing file instead of duplicating it.
"""

from __future__ import annotations

import hashlib
import re

from runtime.tool_base import Tool, ToolContext, ToolResult, resolve_in_roots, work_roots

_MAX_TEXT_CHARS = 5_000_000


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip())[:40].strip("-.")
    return s or "staged"


class ContextStage(Tool):
    name = "context.stage"
    description = (
        "Move oversized text OUT of the conversation into a workspace file and "
        "get back its path. Use when a tool result or draft is too big to keep "
        "quoting: stage it, then ADDRESS the file programmatically (slice it "
        "with code.execute, read ranges with fs.read) instead of re-reading it "
        "whole into your context. Identical text stages to the same file "
        "(content-hashed name), so re-staging is free. Do NOT read the staged "
        "file back unless you need a specific slice."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string",
                     "description": "The full text to stage into a file."},
            "name": {"type": "string",
                     "description": "Optional filename hint (a short slug), e.g. "
                                    "'build-log' or 'api-dump'."},
        },
        "required": ["text"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="context.stage needs a non-empty 'text' string")
        if len(text) > _MAX_TEXT_CHARS:
            return ToolResult(
                status="error", result=None, tool_name=self.name,
                error=f"text too large ({len(text)} chars > {_MAX_TEXT_CHARS}) — "
                      "stage it in parts instead")
        roots = work_roots(ctx)
        if not roots:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="no workspace in this run — nothing to stage into")
        digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:10]
        rel = f"staged/{_slug(str(args.get('name') or ''))}-{digest}.txt"
        try:
            p = resolve_in_roots(roots, rel, must_exist=False)
        except PermissionError as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=str(e))
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(text, encoding="utf-8")
        return ToolResult(status="ok", tool_name=self.name, result={
            "path": str(p),
            "chars": len(text),
            "note": "Staged. Address it, don't read it: slice with code.execute "
                    "(llm_query_batched over chunks for LLM work) or read ranges "
                    "with fs.read — keep the bulk out of your context.",
        })
