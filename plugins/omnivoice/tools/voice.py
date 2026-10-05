"""audio.speak + audio.clone — local text-to-speech through the omnivoice
plugin's tts-server (OmniVoice GGUF on omnivoice.cpp).

The server starts on first use and shuts down after an idle keep-warm
window (no slot hibernation — the Q8_0 pair is ~1 GB and fits next to
brain + specialist).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult, resolve_in_roots, work_roots


def _load_server():
    """Import the plugin's server.py by file path, ONCE per process (plugin
    modules are loaded via spec_from_file_location, not as a package — same
    pattern as the imagegen/h5i plugins)."""
    name = "omnivoice_plugin_server"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parents[1] / "server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


async def _deliver(ctx: ToolContext, path: str) -> tuple[str | None, str | None]:
    """Stage the WAV as a user download AND mirror it into the run workspace
    (the server writes it to DATA/audio — OUTSIDE the workspace, where the
    path gate refuses every file tool; same lesson as imagegen). The
    DATA/audio original is deleted once staged: delivery serves from the
    bundle, so keeping it would grow the dir forever.
    Returns (delivered_name, workspace_path)."""
    delivered = None
    try:
        from runtime.outputs import stage_and_bundle
        from runtime.paths import OUTPUTS_DIR
        web_cfg = (ctx.config.get("web", {}) or {})
        manifest = stage_and_bundle(
            web_cfg.get("outputs_dir", str(OUTPUTS_DIR)),
            ctx.request_id, ctx.owner, [path], None,
            int(web_cfg.get("max_output_mb", 200)) * 1024 * 1024)
        if ctx.emit is not None:
            await ctx.emit("output", {
                "run_id": ctx.request_id, "name": manifest["name"],
                "size": manifest["size"], "kind": manifest["kind"]})
        delivered = manifest["name"]
    except Exception:
        pass
    ws_path = None
    try:
        work_root = getattr(ctx, "work_root", None)
        if work_root:
            import shutil
            dest = Path(work_root) / Path(path).name
            if not dest.exists():
                shutil.copyfile(path, dest)
            ws_path = str(dest)
    except Exception:
        pass
    # The DATA/audio original is redundant once staged (delivery serves
    # from the bundle; the workspace has its own mirror) — drop it so the
    # dir doesn't grow forever. Kept when staging failed: it's the only
    # artifact then.
    if delivered is not None:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
    return delivered, ws_path


class AudioSpeak(Tool):
    name = "audio.speak"
    read_only = False
    description = (
        "Turn text into speech locally (OmniVoice on this machine — 600+ "
        "languages, no cloud, nothing leaves the box). Writes a WAV and "
        "hands it to the user as a download. Use a registered clone "
        "(audio.clone) via `voice`, or shape an anonymous voice with "
        "`instructions` (voice design: gender, age, pitch, emotion, "
        "whisper…). Non-verbal symbols like [laughter] work in the text."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string",
                     "description": "What to say. [laughter] and friends "
                                    "work; pronunciation can be nudged "
                                    "with pinyin/phonemes."},
            "voice": {"type": "string",
                      "description": "Registered cloned voice (see "
                                     "audio.clone list). Omit for voice "
                                     "design / the configured default."},
            "language": {"type": "string",
                         "description": "e.g. English, German, Chinese "
                                        "(default: the configured one)."},
            "instructions": {"type": "string",
                             "description": "Voice design when no clone is "
                                            "used — comma-separated items "
                                            "from OmniVoice's FIXED "
                                            "vocabulary, one per category: "
                                            "male|female, child|teenager|"
                                            "young adult|middle-aged|elderly, "
                                            "very low|low|moderate|high|very "
                                            "high pitch, whisper, or an "
                                            "accent like 'british accent' "
                                            "(american/australian/indian/…). "
                                            "e.g. 'male, very low pitch' or "
                                            "'female, young adult, whisper'. "
                                            "Free prose ('deep voice', "
                                            "'calm, slow') is REJECTED by "
                                            "the server."},
            "seed": {"type": "integer",
                     "description": "Fix for reproducibility (optional)."},
            "keep_warm_s": {"type": "number",
                            "description": "Seconds to keep the TTS "
                                           "backend loaded (default 600)."},
        },
        "required": ["text"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        text = (args.get("text") or "").strip()
        if not text:
            return ToolResult(status="error", result=None,
                              tool_name=self.name,
                              error="text may not be empty")
        mod = _load_server()
        try:
            out = await mod.SERVER.speak(ctx.config, args)
        except mod.OmnivoiceError as e:
            return ToolResult(status="error", result=None,
                              tool_name=self.name, error=str(e))
        delivered, ws_path = await _deliver(ctx, out["path"])
        return ToolResult(
            status="ok", tool_name=self.name,
            result={
                "status": "ok",
                "path": ws_path or out["path"],
                "delivered": delivered,
                "bytes": out["bytes"],
                "voice": out["voice"],
                "language": out["language"],
                "note": ("audio ALREADY handed to the user as a download — "
                         "do NOT call deliver.files; just mention it in "
                         "your reply" if delivered else
                         f"audio written to {out['path']} — deliver it "
                         "with deliver.files")
                        + (f" — workspace copy at {ws_path} for follow-up"
                           if ws_path else "")
                        + f" — the backend shuts down after "
                        f"{out['keep_warm_s']:.0f}s idle",
            })


class AudioClone(Tool):
    name = "audio.clone"
    read_only = False
    description = (
        "Manage cloned voices for audio.speak (OmniVoice, local). "
        "register: add a voice from a reference WAV + its transcript (a "
        "few clear seconds suffice — the user must have the right to "
        "clone this voice). list: the registered names."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["register", "list"],
                       "description": "register: add a voice. list: show "
                                      "registered names."},
            "name": {"type": "string",
                     "description": "register: the voice name (letters, "
                                    "digits, _ and -)."},
            "ref_audio": {"type": "string",
                          "description": "register: WAV path inside the "
                                         "workspace (clear speech, a few "
                                         "seconds)."},
            "ref_text": {"type": "string",
                         "description": "register: the EXACT transcript "
                                        "of the reference audio — it "
                                        "anchors the clone."},
        },
        "required": ["action"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        mod = _load_server()
        if args["action"] == "list":
            try:
                names = await mod.SERVER.voices(ctx.config)
            except mod.OmnivoiceError as e:
                return ToolResult(status="error", result=None,
                                  tool_name=self.name, error=str(e))
            return ToolResult(status="ok", tool_name=self.name,
                              result={"voices": names})
        name = str(args.get("name") or "").strip()
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="name: letters, digits, _ and - only "
                                    "(1-64 chars)")
        ref_text = str(args.get("ref_text") or "").strip()
        if not ref_text:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="ref_text (the exact transcript) is "
                                    "required — it anchors the clone")
        ref_audio = str(args.get("ref_audio") or "").strip()
        if not ref_audio:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="ref_audio (WAV path in the workspace) "
                                    "is required")
        try:
            p = resolve_in_roots(work_roots(ctx), ref_audio)
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None,
                              tool_name=self.name, error=str(e))
        wav = p.read_bytes()
        if len(wav) < 44 or wav[:4] != b"RIFF":
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"{p.name} is not a WAV file — convert "
                                    "first (e.g. with ffmpeg)")
        try:
            out = await mod.SERVER.clone(ctx.config, name, ref_text, wav)
        except mod.OmnivoiceError as e:
            return ToolResult(status="error", result=None,
                              tool_name=self.name, error=str(e))
        return ToolResult(status="ok", tool_name=self.name, result={
            "status": "ok", "voice": out["name"],
            "note": f"voice '{out['name']}' registered on the tts-server — "
                    "use it via audio.speak voice=... Registered voices "
                    "live in server RAM: they are gone when the backend "
                    "shuts down (re-register after a restart).",
        })
