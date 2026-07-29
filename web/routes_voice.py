"""Browser voice I/O routes: STT via a whisper.cpp server, TTS via a local
command (e.g. Piper). Separate from /api/voice (the text-in/text-out channel
for native clients)."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request, Response

_MAX_TTS_CHARS = 4000


async def _whisper_transcribe(url: str, body: bytes, timeout_s: float) -> str:
    """Forward 16 kHz mono WAV to the whisper.cpp server, return raw text."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(url,
                                  files={"file": ("audio.wav", body, "audio/wav")},
                                  data={"response_format": "json"})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502,
                            detail=f"whisper server unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=502,
                            detail=f"whisper server returned {r.status_code}")
    try:
        data = r.json()
        return data.get("text", "") if isinstance(data, dict) else ""
    except ValueError:
        return r.text          # plain-text fallback


async def _run_tts(command_template: str, text: str, timeout_s: float) -> bytes:
    """Run the configured TTS command (text on stdin, {out} = wav path)."""
    if "{out}" not in command_template:
        raise HTTPException(status_code=500,
                            detail="tts command template lacks the {out} placeholder")
    tmpdir = tempfile.mkdtemp(prefix="tts-")
    try:
        out = os.path.join(tmpdir, "out.wav")
        command = command_template.replace("{out}", shlex.quote(out))
        proc = await asyncio.create_subprocess_shell(
            command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(input=text.encode()), timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(status_code=502,
                                detail=f"tts command timed out after {timeout_s}s")
        if proc.returncode != 0:
            tail = (stderr or b"").decode(errors="replace").strip()[-300:]
            raise HTTPException(status_code=502,
                                detail=f"tts command failed ({proc.returncode}): {tail}")
        try:
            return Path(out).read_bytes()
        except OSError:
            raise HTTPException(status_code=502,
                                detail="tts command produced no output file")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _probe_stt(url: str) -> bool:
    """Any HTTP response from the server's root counts as reachable."""
    try:
        p = urlparse(url)
        async with httpx.AsyncClient(timeout=3) as client:
            await client.get(f"{p.scheme}://{p.netloc}/")
        return True
    except Exception:
        return False


def _tts_command_ok(command_template: str) -> bool:
    try:
        return bool(shutil.which(shlex.split(command_template)[0]))
    except (IndexError, ValueError):
        return False


def register(app, s):
    runtime = s.runtime
    max_upload_mb = s.max_upload_mb

    def _voice_cfg() -> dict:
        # Read live so admin config overrides take effect without a restart.
        return runtime.config.get("voice", {}) or {}

    @app.post("/api/stt")
    async def stt(request: Request):
        cfg = _voice_cfg().get("stt", {}) or {}
        if not cfg.get("enabled", False):
            raise HTTPException(status_code=404, detail="stt disabled")
        body = await request.body()
        if len(body) > max_upload_mb * 1024 * 1024:
            raise HTTPException(status_code=413,
                                detail=f"file exceeds {max_upload_mb} MB limit")
        if not body:
            raise HTTPException(status_code=400, detail="empty audio")
        text = await _whisper_transcribe(cfg.get("url", ""), body,
                                         cfg.get("timeout_s", 60))
        return {"text": text.strip()}

    @app.post("/api/tts")
    async def tts(request: Request):
        cfg = _voice_cfg().get("tts", {}) or {}
        if not cfg.get("enabled", False):
            raise HTTPException(status_code=404, detail="tts disabled")
        body = await request.json()
        text = (body.get("text") or "").strip() if isinstance(body, dict) else ""
        if not text:
            raise HTTPException(status_code=400, detail="empty text")
        wav = await _run_tts(cfg.get("command", ""), text[:_MAX_TTS_CHARS],
                             cfg.get("timeout_s", 30))
        return Response(content=wav, media_type="audio/wav")

    @app.get("/api/voice/status")
    async def voice_status(probe: int = 0):
        vcfg = _voice_cfg()
        stt_cfg = vcfg.get("stt", {}) or {}
        tts_cfg = vcfg.get("tts", {}) or {}
        out = {"stt": {"enabled": bool(stt_cfg.get("enabled", False))},
               "tts": {"enabled": bool(tts_cfg.get("enabled", False))}}
        if probe:
            out["stt"]["reachable"] = await _probe_stt(stt_cfg.get("url", ""))
            out["tts"]["command_ok"] = _tts_command_ok(tts_cfg.get("command", ""))
        return out
