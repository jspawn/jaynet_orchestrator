"""Browser voice I/O: /api/stt (whisper.cpp proxy), /api/tts (local command,
e.g. Piper) and /api/voice/status. Everything external is monkeypatched or
faked with a `cp` command template — no real whisper server or TTS binary.
"""

import shlex
import wave

import httpx
import pytest

import web.routes_voice as routes_voice


async def _set_config(c, **updates):
    r = await c.put("/api/admin/config", json={"updates": updates})
    assert r.status_code == 200


def _write_wav(path):
    """A tiny valid 16 kHz mono wav, returns its bytes."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * 1600)
    return path.read_bytes()


# ---- /api/stt ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_stt_disabled_404(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.post("/api/stt", content=b"RIFF-fake")
        assert r.status_code == 404
        assert r.json()["detail"] == "stt disabled"


@pytest.mark.asyncio
async def test_stt_transcribes(web_app, web_client, monkeypatch):
    app = web_app()
    seen = {}

    async def fake_whisper(url, body, timeout_s):
        seen.update(url=url, body=body, timeout_s=timeout_s)
        return "  hello world  "

    monkeypatch.setattr(routes_voice, "_whisper_transcribe", fake_whisper)
    async with web_client(app) as c:
        await _set_config(c, **{"voice.stt.enabled": True,
                                "voice.stt.url": "http://127.0.0.1:8097/inference"})
        r = await c.post("/api/stt", content=b"RIFF-fake-wav")
        assert r.status_code == 200
        assert r.json() == {"text": "hello world"}     # stripped
        assert seen["url"] == "http://127.0.0.1:8097/inference"
        assert seen["body"] == b"RIFF-fake-wav"


# ---- /api/tts ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_tts_disabled_404(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.post("/api/tts", json={"text": "hi"})
        assert r.status_code == 404
        assert r.json()["detail"] == "tts disabled"


@pytest.mark.asyncio
async def test_tts_empty_400(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        await _set_config(c, **{"voice.tts.enabled": True})
        r = await c.post("/api/tts", json={"text": "   "})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_tts_monkeypatched(web_app, web_client, monkeypatch):
    app = web_app()
    wav = b"RIFF" + b"\x00" * 40

    async def fake_tts(command, text, timeout_s):
        assert len(text) <= 4000
        return wav

    monkeypatch.setattr(routes_voice, "_run_tts", fake_tts)
    async with web_client(app) as c:
        await _set_config(c, **{"voice.tts.enabled": True,
                                "voice.tts.command": "piper --output_file {out}"})
        r = await c.post("/api/tts", json={"text": "say this"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("audio/wav")
        assert r.content == wav


@pytest.mark.asyncio
async def test_tts_real_command(tmp_path, web_app, web_client):
    """End-to-end with a real shell command: `cp <fixture> {out}`."""
    app = web_app()
    wav = _write_wav(tmp_path / "fixture.wav")
    cmd = f"cp {shlex.quote(str(tmp_path / 'fixture.wav'))} {{out}}"
    async with web_client(app) as c:
        await _set_config(c, **{"voice.tts.enabled": True,
                                "voice.tts.command": cmd})
        r = await c.post("/api/tts", json={"text": "hello"})
        assert r.status_code == 200
        assert r.content == wav


@pytest.mark.asyncio
async def test_tts_command_failure_502(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        await _set_config(c, **{"voice.tts.enabled": True,
                                "voice.tts.command": "cp /nonexistent-src-xyz {out}"})
        r = await c.post("/api/tts", json={"text": "hello"})
        assert r.status_code == 502
        assert "tts command failed" in r.json()["detail"]


@pytest.mark.asyncio
async def test_tts_missing_placeholder_500(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        await _set_config(c, **{"voice.tts.enabled": True,
                                "voice.tts.command": "true"})
        r = await c.post("/api/tts", json={"text": "hello"})
        assert r.status_code == 500
        assert "{out}" in r.json()["detail"]


# ---- /api/voice/status -------------------------------------------------------
@pytest.mark.asyncio
async def test_voice_status_shape(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/voice/status")
        assert r.status_code == 200
        assert r.json() == {"stt": {"enabled": False}, "tts": {"enabled": False}}

        await _set_config(c, **{"voice.stt.enabled": True,
                                "voice.tts.enabled": True})
        r = await c.get("/api/voice/status")
        assert r.json() == {"stt": {"enabled": True}, "tts": {"enabled": True}}


@pytest.mark.asyncio
async def test_voice_status_probe(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        # Nonsense url + nonsense binary: nothing to reach, nothing on PATH.
        await _set_config(c, **{"voice.stt.enabled": True,
                                "voice.stt.url": "http://127.0.0.1:1/x",
                                "voice.tts.enabled": True,
                                "voice.tts.command": "not-a-real-binary-xyz --m {out}"})
        r = await c.get("/api/voice/status?probe=1")
        assert r.status_code == 200
        d = r.json()
        assert d["stt"] == {"enabled": True, "reachable": False}
        assert d["tts"] == {"enabled": True, "command_ok": False}


# ---- auth --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stt_requires_auth(web_app):
    app = web_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/stt", content=b"RIFF-fake")
        assert r.status_code == 401
