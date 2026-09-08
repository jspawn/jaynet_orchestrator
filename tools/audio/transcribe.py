"""Audio tools — speech-to-text via a local whisper.cpp whisper-server.

The `stt` preset slot runs whisper-server (a WHISPER=on preset launched by
start-model.sh, CPU-only like embed/rerank). It speaks its own multipart
/inference HTTP API — not OpenAI chat — so the tool posts to it DIRECTLY
(tools.audio.stt_url), not through LiteLLM, the same pattern rag.* uses for
the embed/rerank helpers. While the stt slot is empty the server is down and
the tool returns a clear "assign a whisper preset" error.

Marked private: transcripts of the user's own audio never flow to a cloud LLM
unless the run allows private sharing.
"""

from __future__ import annotations

import httpx

from runtime.tool_base import (
    Tool,
    ToolContext,
    ToolResult,
    resolve_in_roots,
    work_roots,
)


def _stt_url(ctx: ToolContext) -> str:
    return (str((ctx.config.get("tools", {}).get("audio", {}) or {})
                .get("stt_url") or "").strip()
            or "http://127.0.0.1:8099/inference")


_STT_DOWN = ("the speech-to-text endpoint is not reachable ({err}). The stt "
             "slot is not running or has no preset assigned — assign a "
             "whisper preset (e.g. presets/stt-whisper-large-v3-turbo.conf) "
             "to the 'stt' slot in Admin → Presets (Boot model slots), then "
             "retry.")


class AudioTranscribe(Tool):
    name = "audio.transcribe"
    description = ("Transcribe an audio file (wav/mp3/ogg/flac/m4a/…) to text "
                   "with the local whisper server. Pass a workspace `path`; "
                   "optionally hint the spoken `language` (e.g. 'en', 'de'). "
                   "Needs the stt slot (whisper) running.")
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Path to the audio file (inside your workspace)."},
            "language": {"type": "string",
                         "description": "Optional ISO language hint (en, de, …). "
                                        "Omit for auto-detection."},
        },
        "required": ["path"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        # Confine exactly like the fs.* tools: only files under this run's
        # work roots may be read and sent to the server.
        try:
            p = resolve_in_roots(work_roots(ctx), args["path"])
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(status="error", result=None, error=str(e))
        if not p.is_file():
            return ToolResult(status="error", result=None,
                              error=f"not a file: {p}")

        data = {"response-format": "json"}
        lang = str(args.get("language") or "").strip()
        if lang:
            data["language"] = lang
        url = _stt_url(ctx)
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                with p.open("rb") as fh:
                    r = await client.post(
                        url, data=data, files={"file": (p.name, fh)})
                r.raise_for_status()
                payload = r.json()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            return ToolResult(status="error", result=None,
                              error=_STT_DOWN.format(err=f"{type(e).__name__}: {e}"))
        except httpx.HTTPStatusError as e:
            return ToolResult(status="error", result=None,
                              error=_STT_DOWN.format(
                                  err=f"HTTP {e.response.status_code}: "
                                      f"{e.response.text[:300]}"))
        except Exception as e:
            return ToolResult(status="error", result=None,
                              error=f"transcription failed: {type(e).__name__}: {e}")

        text = str(payload.get("text") or "").strip()
        if not text:
            return ToolResult(status="ok", result={
                "text": "", "file": p.name,
                "note": "the server returned an empty transcript — the audio "
                        "may be silent, or not speech"})
        return ToolResult(status="ok", result={"text": text, "file": p.name,
                                               "language": lang or "auto"})
