"""The bare-'test' smoke-test fast-path (web/routes_run.py): a first-message
'test' is answered by a model-endpoint liveness probe, not an agent run —
anything else (project chats, continued conversations, longer messages) still
reaches the loop, so real test work via skills/tools is unaffected."""

import asyncio

import pytest


async def _chat_reply(c, payload):
    """POST /api/chat and read the whole SSE replay (the smoke reply is a
    model-less canned run, same shape as the fast-path)."""
    r = await c.post("/api/chat", json=payload)
    assert r.status_code == 200
    rid = r.json()["run_id"]
    r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert r.status_code == 200
    return r.text


def _probe_ok(_base):
    async def f():
        return "qwen3-4b-q4_k_m.gguf"
    return f()


@pytest.mark.asyncio
async def test_bare_test_first_message_probes_endpoint(web_app, web_client,
                                                       record_run, monkeypatch):
    monkeypatch.setattr("web.routes_run._probe_model_endpoint",
                        lambda base: _probe_ok(base))
    app = web_app()
    seen = record_run(app)
    async with web_client(app) as c:
        body = await _chat_reply(c, {"message": "test"})
    assert "Smoke test passed" in body
    assert "qwen3-4b-q4_k_m.gguf" in body
    assert not seen, "bare 'test' must never reach the agent loop"


@pytest.mark.asyncio
async def test_bare_test_endpoint_down_reports_failure(web_app, web_client,
                                                       record_run, monkeypatch):
    async def down(base):
        raise ConnectionError("refused")
    monkeypatch.setattr("web.routes_run._probe_model_endpoint", down)
    app = web_app()
    seen = record_run(app)
    async with web_client(app) as c:
        body = await _chat_reply(c, {"message": "Test!"})
    assert "Smoke test failed" in body
    assert "start.sh" in body
    assert not seen


@pytest.mark.asyncio
async def test_test_in_project_reaches_loop(web_app, web_client, record_run,
                                            monkeypatch):
    # In a project, "test" means "run the tests" — never intercept it.
    monkeypatch.setattr("web.routes_run._probe_model_endpoint",
                        lambda base: _probe_ok(base))
    app = web_app()
    seen = record_run(app)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "test",
                                            "project_id": "no-such-project"})
        assert r.status_code == 200
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.02)
    assert seen, "'test' inside a project must reach the agent loop"


@pytest.mark.asyncio
async def test_test_with_history_reaches_loop(web_app, web_client, record_run,
                                              monkeypatch):
    monkeypatch.setattr("web.routes_run._probe_model_endpoint",
                        lambda base: _probe_ok(base))
    app = web_app()
    seen = record_run(app)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={
            "message": "test",
            "history": [{"role": "user", "content": "hi"},
                        {"role": "assistant", "content": "hello"}]})
        assert r.status_code == 200
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.02)
    assert seen, "'test' mid-conversation must reach the agent loop"


@pytest.mark.asyncio
async def test_longer_test_message_reaches_loop(web_app, web_client, record_run,
                                                monkeypatch):
    monkeypatch.setattr("web.routes_run._probe_model_endpoint",
                        lambda base: _probe_ok(base))
    app = web_app()
    seen = record_run(app)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "test my login form"})
        assert r.status_code == 200
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.02)
    assert seen, "a real test request must reach the agent loop"
