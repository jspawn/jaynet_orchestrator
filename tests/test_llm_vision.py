"""llm.call images: multimodal content blocks, vision-model routing,
image-file validation (type/size/confinement), and the actionable error when
the local vision slot is down.

All LiteLLM HTTP is mocked (httpx.AsyncClient).
"""
import asyncio
import base64

import httpx
import pytest

import tools.llm.cloud_models as M
from runtime.tool_base import ToolContext
from tools.llm.cloud_models import CallCloudLLM, resolve_model_alias


class _Resp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://x/v1/chat/completions")
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

    async def post(self, url, json=None, headers=None):
        if type(self).raise_exc is not None:
            raise type(self).raise_exc
        type(self).posts.append({"url": url, "json": json, "headers": headers})
        return type(self).response


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.posts = []
    _FakeClient.raise_exc = None
    _FakeClient.response = _Resp({
        "choices": [{"message": {"content": "A red barn in a field."}}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 12},
    })
    monkeypatch.setattr(M.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _run(args, ctx):
    return asyncio.run(CallCloudLLM().execute(args, ctx))


def _ctx(tmp_path, config=None):
    return ToolContext(request_id="t", config=config or {}, budget=None,
                       work_root=tmp_path)


def test_images_build_multimodal_content_blocks(fake_http, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    r = _run({"task": "What is in this image?", "images": ["shot.png"]},
             _ctx(tmp_path))
    assert r.status == "ok" and r.result == "A red barn in a field."
    body = fake_http.posts[0]["json"]
    # no explicit model + images → the local vision slot
    assert body["model"] == "local-vision"
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "What is in this image?"}
    assert content[1]["type"] == "image_url"
    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == img.read_bytes()


def test_data_url_entries_pass_through(fake_http, tmp_path):
    du = "data:image/webp;base64," + base64.b64encode(b"RIFF....").decode()
    r = _run({"task": "describe", "images": [du]}, _ctx(tmp_path))
    assert r.status == "ok"
    content = fake_http.posts[0]["json"]["messages"][0]["content"]
    assert content[1]["image_url"]["url"] == du


def test_explicit_model_with_images_is_allowed(fake_http, tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff" + b"y" * 16)
    r = _run({"model": "gemini", "task": "describe", "images": ["a.jpg"]},
             _ctx(tmp_path))
    assert r.status == "ok"
    body = fake_http.posts[0]["json"]
    assert body["model"] == "gemini-pro"          # explicit model wins
    assert body["messages"][0]["content"][1]["image_url"]["url"] \
        .startswith("data:image/jpeg;base64,")


def test_vision_model_config_override(fake_http, tmp_path):
    (tmp_path / "a.png").write_bytes(b"\x89PNG" + b"z" * 8)
    cfg = {"tools": {"llm": {"vision_model": "local-specialist"}}}
    r = _run({"task": "describe", "images": ["a.png"]}, _ctx(tmp_path, cfg))
    assert r.status == "ok"
    assert fake_http.posts[0]["json"]["model"] == "local-specialist"


def test_text_call_still_requires_model(fake_http, tmp_path):
    r = _run({"task": "hello"}, _ctx(tmp_path))
    assert r.status == "error" and "model is required" in r.error
    assert fake_http.posts == []


def test_oversized_image_refused(fake_http, tmp_path):
    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG" + b"0" * (10 * 1024 * 1024 + 1))
    r = _run({"task": "describe", "images": ["big.png"]}, _ctx(tmp_path))
    assert r.status == "error"
    assert "too large" in r.error and "10MB" in r.error
    assert fake_http.posts == []


def test_missing_and_unsupported_and_outside_images(fake_http, tmp_path):
    (tmp_path / "notes.txt").write_text("not an image")
    r = _run({"task": "x", "images": ["ghost.png"]}, _ctx(tmp_path))
    assert r.status == "error" and "no such file" in r.error
    r = _run({"task": "x", "images": ["notes.txt"]}, _ctx(tmp_path))
    assert r.status == "error" and "unsupported image type" in r.error
    r = _run({"task": "x", "images": ["/etc/hostname"]}, _ctx(tmp_path))
    assert r.status == "error" and "outside your workspace" in r.error
    assert fake_http.posts == []


def test_vision_endpoint_down_gives_actionable_error(fake_http, tmp_path):
    (tmp_path / "a.png").write_bytes(b"\x89PNG" + b"z" * 8)
    _FakeClient.raise_exc = httpx.ConnectError("Connection refused")
    r = _run({"task": "describe", "images": ["a.png"]}, _ctx(tmp_path))
    assert r.status == "error"
    assert "vision slot" in r.error and "Admin" in r.error
    assert "ConnectError" in r.error                # underlying error kept
    # the same failure on a non-vision target keeps the plain error
    r = _run({"model": "gemini", "task": "describe", "images": ["a.png"]},
             _ctx(tmp_path))
    assert r.status == "error" and "vision slot" not in r.error


def test_vision_endpoint_404_gives_actionable_error(fake_http, tmp_path):
    (tmp_path / "a.png").write_bytes(b"\x89PNG" + b"z" * 8)
    _FakeClient.response = _Resp(status=404, text="model not found")
    r = _run({"task": "describe", "images": ["a.png"]}, _ctx(tmp_path))
    assert r.status == "error"
    assert "vision slot" in r.error and "HTTP 404" in r.error


def test_local_vision_alias_resolution():
    assert resolve_model_alias("local-vision") == "local-vision"
    assert resolve_model_alias("LOCAL_VISION") == "local-vision"
    enum = CallCloudLLM().parameters["properties"]["model"]["enum"]
    assert "local-vision" in enum
