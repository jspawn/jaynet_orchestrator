"""image.generate — local text-to-image through the imagegen plugin's
sd-server (Qwen-Image GGUF on stable-diffusion.cpp).

The tool hibernates the configured model slot (default: specialist) for
the duration of the generation and restores it after an idle keep-warm
window, so a burst of image requests pays the model swap only once.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult


def _load_server():
    """Import the plugin's server.py by file path, ONCE per process (plugin
    modules are loaded via spec_from_file_location, not as a package — same
    pattern as the benchlab/graphify plugins)."""
    name = "imagegen_plugin_server"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parents[1] / "server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


class ImageGenerate(Tool):
    name = "image.generate"
    read_only = False
    description = (
        "Generate an image locally from a text prompt (Qwen-Image on this "
        "machine — no cloud, nothing leaves the box). Writes a PNG and "
        "hands it to the user as a download, and returns its path. NOTE: "
        "this hibernates the "
        "specialist model for the duration (it restarts automatically after "
        "an idle keep-warm window), so do not call it while a delegation "
        "needs the specialist."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string",
                       "description": "What to draw — be specific about "
                                      "subject, style, composition, text "
                                      "to render (Qwen-Image is good at "
                                      "rendering quoted text)."},
            "negative_prompt": {"type": "string",
                                "description": "What to avoid (optional)."},
            "size": {"type": "string",
                     "description": "WxH, e.g. 1024x1024 (default), "
                                    "1328x1328, 1664x928, 928x1664."},
            "steps": {"type": "integer",
                      "description": "Sampling steps (default 20; more = "
                                     "slower/finer)."},
            "seed": {"type": "integer",
                     "description": "Fix for reproducibility (optional)."},
            "keep_warm_s": {"type": "number",
                            "description": "Seconds to keep the image "
                                           "backend loaded before restoring "
                                           "the specialist (default 600)."},
        },
        "required": ["prompt"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(status="error", result=None,
                              tool_name=self.name,
                              error="prompt may not be empty")
        mod = _load_server()
        try:
            out = await mod.SERVER.generate(ctx.config, args)
        except mod.ImagegenError as e:
            return ToolResult(status="error", result=None,
                              tool_name=self.name, error=str(e))
        # Stage the PNG as a user download right away (same machinery as
        # deliver.files): the artifact lives in DATA/images — OUTSIDE the run
        # workspace — so a deliver.files call on it is refused (live: the
        # first smoke generation errored exactly there). Best-effort: the
        # PNG on disk is the artifact; the download chip is a convenience.
        delivered = None
        try:
            from runtime.outputs import stage_and_bundle
            from runtime.paths import OUTPUTS_DIR
            web_cfg = (ctx.config.get("web", {}) or {})
            manifest = stage_and_bundle(
                web_cfg.get("outputs_dir", str(OUTPUTS_DIR)),
                ctx.request_id, ctx.owner, [out["path"]], None,
                int(web_cfg.get("max_output_mb", 200)) * 1024 * 1024)
            if ctx.emit is not None:
                await ctx.emit("output", {
                    "run_id": ctx.request_id, "name": manifest["name"],
                    "size": manifest["size"], "kind": manifest["kind"]})
            delivered = manifest["name"]
        except Exception:
            pass
        # Mirror the PNG into the run workspace so follow-up tools (fs.*,
        # llm.call vision check, deliver.files) can touch it — the server
        # writes it to DATA/images, OUTSIDE the workspace, where the
        # path gate refuses every file tool (live: a brain burned several
        # calls `cp`-ing it over by hand after deliver.files refused).
        ws_path = None
        try:
            work_root = getattr(ctx, "work_root", None)
            if work_root:
                import shutil
                src = Path(out["path"])
                dest = Path(work_root) / src.name
                # Mirror when missing OR stale: a regenerated artifact
                # reusing a name must still reach the workspace, not
                # silently keep the old bytes.
                if not dest.exists() \
                        or dest.stat().st_size != src.stat().st_size \
                        or dest.read_bytes() != src.read_bytes():
                    shutil.copyfile(src, dest)
                ws_path = str(dest)
        except Exception:
            pass
        import base64
        data_url = "data:image/png;base64," + base64.b64encode(
            Path(out["path"]).read_bytes()).decode()
        # The DATA/images original is redundant once staged AND mirrored
        # (delivery serves from the bundle; the workspace has its own
        # copy) — drop it so the dir doesn't grow forever. Kept when either
        # failed: the returned path (ws_path or the original) must always
        # exist.
        if delivered is not None and ws_path is not None:
            try:
                Path(out["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        return ToolResult(
            status="ok", tool_name=self.name,
            result={
                "status": "ok",
                "path": ws_path or out["path"],
                "delivered": delivered,
                "bytes": out["bytes"],
                "size": out["size"],
                "steps": out["steps"],
                "note": ("image ALREADY handed to the user as a download — "
                         "do NOT call deliver.files; just mention the file "
                         "in your reply" if delivered else
                         f"image written to {out['path']} — deliver it with "
                         "deliver.files")
                        + (f" — workspace copy at {ws_path} for follow-up "
                           "(vision check, edits)" if ws_path else "")
                        + f" — the {out['slot_hibernated']} slot restarts "
                        f"automatically after {out['keep_warm_s']:.0f}s "
                        "idle; the image is attached so you can check it "
                        "against the prompt (vision brains only)",
            },
            images=[data_url])
