"""Deliver files back to the user — `deliver.files`.

Hands one or more files (or folders) you've produced to the user as a download in
the web client. A single file is offered as-is; multiple files or any folder are
bundled into one `.tar.gz`. The bytes are staged server-side and surfaced as a
download link on this turn; they're kept only if the user saves the chat (and
swept otherwise), so this is the right way to return generated artifacts.

Not private (the user asked for these and they go only to that authenticated user,
never to a remote LLM) and not confirmation-gated (delivering to the human in the
loop is the benign end of a task).
"""

from __future__ import annotations

from runtime.outputs import OutputTooLarge, stage_and_bundle
from runtime.tool_base import Tool, ToolContext, ToolResult


def _cfg(ctx: ToolContext) -> dict:
    return (ctx.config.get("web", {}) or {})


class DeliverFiles(Tool):
    name = "deliver.files"
    description = (
        "Give one or more files (or folders) back to the user as a download in the "
        "web client. Pass the absolute path(s) of artifacts you've produced (e.g. an "
        "edited document, a generated report, a folder of results). A single file is "
        "delivered as-is; multiple files or any folder are bundled into one .tar.gz. "
        "Call this once with everything you want to hand over; mention in your reply "
        "that the download is ready."
    )
    parameters = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array", "items": {"type": "string"},
                "description": "Absolute path(s) of the file(s)/folder(s) to deliver.",
            },
            "name": {
                "type": "string",
                "description": "Optional download name for the bundle (used when "
                               "multiple items are bundled into a .tar.gz).",
            },
        },
        "required": ["paths"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        paths = args.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if not paths:
            return ToolResult(status="error", result=None, error="paths is required")
        cfg = _cfg(ctx)
        outputs_dir = cfg.get("outputs_dir", "/srv/orchestrator/data/outputs")
        max_mb = int(cfg.get("max_output_mb", 200))
        try:
            manifest = stage_and_bundle(outputs_dir, ctx.request_id, ctx.owner,
                                        list(paths), args.get("name"),
                                        max_mb * 1024 * 1024)
        except FileNotFoundError as e:
            return ToolResult(status="error", result=None,
                              error=f"path not found: {e}")
        except OutputTooLarge as e:
            return ToolResult(status="error", result=None,
                              error=f"delivery too large ({e.size} bytes; limit "
                                    f"{max_mb} MB) — deliver fewer/smaller files")
        # Surface a live download chip on this turn (no-op on the CLI path).
        if ctx.emit is not None:
            await ctx.emit("output", {
                "run_id": ctx.request_id, "name": manifest["name"],
                "size": manifest["size"], "kind": manifest["kind"],
            })
        return ToolResult(status="ok", result={
            "delivered": manifest["name"], "kind": manifest["kind"],
            "size": manifest["size"], "download_url": f"/api/output/{ctx.request_id}",
            "note": "Download offered to the user; kept only if they save the chat.",
        })
