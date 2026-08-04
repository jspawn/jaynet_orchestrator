"""Stable API contract (docs/api.md) — pins the response shapes native
clients depend on. If one of these fails, either the change is breaking
(needs a minor version bump + CHANGELOG entry) or the test is right and the
code regressed."""
import httpx
import pytest

import runtime


@pytest.mark.asyncio
async def test_health_shape(web_app):
    app = web_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        body = (await c.get("/api/health")).json()
    assert set(body) >= {"ok", "version", "tools"}
    assert body["ok"] is True and body["version"] == runtime.__version__


@pytest.mark.asyncio
async def test_auth_required_and_bearer_accepted(web_app):
    app = web_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.post("/api/chat", json={"message": "hi"})).status_code == 401
        tok = app.state.users.create_api_token("admin", "contract")["token"]
        r = await c.post("/api/chat", json={"message": "hi"},
                         headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_chat_returns_run_id(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    async with web_client(app) as c:
        body = (await c.post("/api/chat", json={"message": "hi"})).json()
    assert set(body) >= {"run_id"} and body["run_id"]


@pytest.mark.asyncio
async def test_voice_shapes(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        body = (await c.post("/api/voice", json={"text": "hi"})).json()
        assert set(body) >= {"conversation_id", "run_id", "text", "status"}
        body = (await c.post("/api/voice",
                             json={"text": "hi", "stream": True})).json()
        assert set(body) >= {"conversation_id", "run_id"}


@pytest.mark.asyncio
async def test_unknown_run_is_404(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        assert (await c.post("/api/cancel/nonexistent")).status_code == 404
        assert (await c.get("/api/stream/nonexistent")).status_code == 404


@pytest.mark.asyncio
async def test_tools_shape(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        body = (await c.get("/api/tools")).json()
    assert isinstance(body["tools"], list)
    if body["tools"]:
        assert set(body["tools"][0]) >= {
            "name", "namespace", "description", "private",
            "requires_confirmation", "parameters", "enabled"}
