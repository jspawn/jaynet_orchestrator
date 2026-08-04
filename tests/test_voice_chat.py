"""Chat mode (voice:false) on /api/voice — server-managed conversation for
native chat clients: no voice persona overlay, thinking on, normal budgets,
while the safe unattended toolset applies to both modes."""
import pytest


@pytest.mark.asyncio
async def test_voice_default_keeps_voice_persona(web_app, web_client, record_run):
    app = web_app()
    seen = record_run(app)
    vcfg = app.state.runtime.config["voice"]
    async with web_client(app) as c:
        r = await c.post("/api/voice", json={"text": "hello"})
        assert r.status_code == 200
    assert seen["think"] is False
    assert seen["extra_system"] == vcfg.get("persona")
    assert seen["budget_overrides"] == (vcfg.get("budget") or None)
    assert seen["model"] == vcfg.get("model")


@pytest.mark.asyncio
async def test_chat_mode_drops_voice_shaping(web_app, web_client, record_run):
    app = web_app()
    seen = record_run(app)
    async with web_client(app) as c:
        r = await c.post("/api/voice", json={"text": "hello", "voice": False})
        assert r.status_code == 200
        assert r.json()["conversation_id"]
    assert seen["think"] is True
    assert seen["extra_system"] is None
    assert seen["budget_overrides"] is None
    assert seen["model"] is None


@pytest.mark.asyncio
async def test_safe_toolset_applies_in_both_modes(web_app, web_client, record_run):
    from runtime.tool_base import Tool

    class _Gated(Tool):
        name = "test.gated"
        description = "d"
        requires_confirmation = True
        parameters = {"type": "object", "properties": {}}

        async def execute(self, args, ctx):
            return {}

    class _Open(_Gated):
        name = "test.open"
        requires_confirmation = False

    app = web_app()
    app.state.runtime.registry.register_instance(_Gated())
    app.state.runtime.registry.register_instance(_Open())
    seen = record_run(app)
    async with web_client(app) as c:
        await c.post("/api/voice", json={"text": "hi"})
        voice_tools = seen["tools"]
        await c.post("/api/voice", json={"text": "hi", "voice": False})
        chat_tools = seen["tools"]
    assert voice_tools == chat_tools
    assert "test.gated" not in chat_tools
    assert "test.open" in chat_tools


@pytest.mark.asyncio
async def test_chat_mode_conversation_continuity(web_app, web_client):
    app = web_app()

    async def fake_run(msg, **kw):
        return {"status": "ok", "answer": f"answer to: {msg}"}
    app.state.runtime.run = fake_run

    async with web_client(app) as c:
        r1 = await c.post("/api/voice", json={"text": "first", "voice": False})
        cid = r1.json()["conversation_id"]
        r2 = await c.post("/api/voice", json={
            "text": "second", "voice": False, "conversation_id": cid})
        assert r2.json()["conversation_id"] == cid
    chat = app.state.chats.get(cid, "admin")
    assert [t["user_message"] for t in chat["turns"]] == ["first", "second"]


@pytest.mark.asyncio
async def test_chat_mode_foreign_conversation_id_ignored(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.post("/api/voice", json={
            "text": "hi", "voice": False, "conversation_id": "not-mine"})
        assert r.status_code == 200
        assert r.json()["conversation_id"] != "not-mine"
