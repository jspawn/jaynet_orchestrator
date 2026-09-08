"""audio.transcribe: multipart POST to the whisper server, transcript parsing,
endpoint-down guidance, and workspace path confinement.

All HTTP is mocked (httpx.AsyncClient).
"""
import asyncio

import httpx
import pytest

import tools.audio.transcribe as T
from runtime.tool_base import ToolContext
from tools.audio.transcribe import AudioTranscribe


class _Resp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://x/inference")
            raise httpx.HTTPStatusError(
                "err", request=req,
                response=httpx.Response(self.status_code, text=self.text, request=req))

    def json(self):
        return self._payload


class _FakeClient:
    posts = []
    response = None
    raise_exc = None

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, data=None, files=None):
        if type(self).raise_exc is not None:
            raise type(self).raise_exc
        type(self).posts.append({"url": url, "data": data, "files": files})
        return type(self).response


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.posts = []
    _FakeClient.raise_exc = None
    _FakeClient.response = _Resp({"text": " hello world "})
    monkeypatch.setattr(T.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _ctx(tmp_path, config=None):
    return ToolContext(request_id="t", config=config or {}, budget=None,
                       work_root=tmp_path)


def _run(args, ctx):
    return asyncio.run(AudioTranscribe().execute(args, ctx))


def test_transcribe_posts_multipart_and_returns_text(fake_http, tmp_path):
    (tmp_path / "note.wav").write_bytes(b"RIFF" + b"\0" * 64)
    cfg = {"tools": {"audio": {"stt_url": "http://127.0.0.1:8099/inference"}}}
    r = _run({"path": "note.wav", "language": "en"}, _ctx(tmp_path, cfg))
    assert r.status == "ok"
    assert r.result["text"] == "hello world"       # stripped
    assert r.result["file"] == "note.wav" and r.result["language"] == "en"
    post = fake_http.posts[0]
    assert post["url"] == "http://127.0.0.1:8099/inference"
    assert post["data"] == {"response-format": "json", "language": "en"}
    assert post["files"]["file"][0] == "note.wav"  # (filename, fh) tuple


def test_language_omitted_when_not_given(fake_http, tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"\xff\xfb" + b"\0" * 32)
    r = _run({"path": "a.mp3"}, _ctx(tmp_path))
    assert r.status == "ok" and r.result["language"] == "auto"
    assert fake_http.posts[0]["data"] == {"response-format": "json"}
    # default URL when tools.audio is not configured
    assert fake_http.posts[0]["url"] == "http://127.0.0.1:8099/inference"


def test_endpoint_down_gives_actionable_error(fake_http, tmp_path):
    (tmp_path / "a.wav").write_bytes(b"RIFF")
    _FakeClient.raise_exc = httpx.ConnectError("Connection refused")
    r = _run({"path": "a.wav"}, _ctx(tmp_path))
    assert r.status == "error"
    assert "stt slot" in r.error and "Admin" in r.error
    assert "ConnectError" in r.error
    # an HTTP failure (server up, request rejected) gets the same guidance
    _FakeClient.raise_exc = None
    _FakeClient.response = _Resp(status=500, text="boom")
    r = _run({"path": "a.wav"}, _ctx(tmp_path))
    assert r.status == "error" and "stt slot" in r.error
    assert "HTTP 500" in r.error


def test_empty_transcript_is_ok_with_note(fake_http, tmp_path):
    (tmp_path / "silence.wav").write_bytes(b"RIFF")
    _FakeClient.response = _Resp({"text": ""})
    r = _run({"path": "silence.wav"}, _ctx(tmp_path))
    assert r.status == "ok" and r.result["text"] == ""
    assert "empty transcript" in r.result["note"]


def test_path_confinement_and_missing_file(fake_http, tmp_path):
    r = _run({"path": "/etc/hostname"}, _ctx(tmp_path))
    assert r.status == "error" and "outside your workspace" in r.error
    r = _run({"path": "ghost.wav"}, _ctx(tmp_path))
    assert r.status == "error" and "no such file" in r.error
    assert fake_http.posts == []
