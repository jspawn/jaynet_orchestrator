"""Mic-button STT routes: /api/stt probes the whisper slot with a cheap TCP
connect (the button only shows when it's up), and POST /api/stt forwards the
browser's client-side-encoded WAV to the slot and returns the transcript.

All whisper HTTP is mocked; TCP probing is monkeypatched.
"""

import pytest


@pytest.mark.asyncio
async def test_stt_status_available_and_down(web_app, web_client, monkeypatch):
    app = web_app()
    import asyncio

    class _W:
        def close(self): pass
        async def wait_closed(self): pass

    async def _ok(*a, **k): return None, _W()
    async def _fail(*a, **k): raise ConnectionRefusedError("down")

    monkeypatch.setattr(asyncio, "open_connection", _ok)
    async with web_client(app) as c:
        assert ((await c.get("/api/stt")).json())["available"] is True
    monkeypatch.setattr(asyncio, "open_connection", _fail)
    async with web_client(app) as c:
        assert ((await c.get("/api/stt")).json())["available"] is False


@pytest.mark.asyncio
async def test_stt_transcribe_forwards_and_parses(web_app, web_client, monkeypatch):
    app = web_app()
    calls = []

    async def _fake(url, filename, data):
        calls.append({"url": url, "filename": filename, "data": data})
        return "hello world"

    import web.routes_chats as RC
    monkeypatch.setattr(RC, "_stt_forward", _fake)

    async with web_client(app) as c:
        r = await c.post("/api/stt",
                         files={"file": ("mic.wav", b"RIFF....", "audio/wav")})
        assert r.status_code == 200, r.text
        assert r.json()["text"] == "hello world"
        assert calls and calls[0]["url"].endswith("/inference")
        assert calls[0]["filename"] == "mic.wav"
        assert calls[0]["data"] == b"RIFF...."


@pytest.mark.asyncio
async def test_stt_transcribe_slot_down_is_503(web_app, web_client, monkeypatch):
    app = web_app()

    async def _down(*a):
        raise ConnectionRefusedError("down")

    import web.routes_chats as RC
    monkeypatch.setattr(RC, "_stt_forward", _down)

    async with web_client(app) as c:
        r = await c.post("/api/stt",
                         files={"file": ("mic.wav", b"RIFF....", "audio/wav")})
        assert r.status_code == 503
        assert "whisper preset" in r.json()["detail"]


@pytest.mark.asyncio
async def test_stt_transcribe_rejects_empty(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.post("/api/stt", files={"file": ("mic.wav", b"", "audio/wav")})
        assert r.status_code == 400
