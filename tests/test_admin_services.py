"""Admin service-restart endpoint (Admin → Status): whitelist-only user units,
self-restart delayed+detached, proxy restart awaited. Subprocesses are faked —
no systemctl ever runs."""
import asyncio
import subprocess

import pytest


@pytest.mark.asyncio
async def test_restart_rejects_unknown_service(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.post("/api/admin/services/restart",
                         json={"service": "nginx"})
        assert r.status_code == 400
        assert "not restartable" in r.json()["detail"]


@pytest.mark.asyncio
async def test_restart_litellm_awaits_systemctl(web_app, web_client, monkeypatch):
    calls = []

    class _Proc:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*args, **kw):
        calls.append(args)
        return _Proc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    app = web_app()
    async with web_client(app) as c:
        r = await c.post("/api/admin/services/restart",
                         json={"service": "litellm-proxy"})
        assert r.status_code == 200 and r.json()["ok"] is True
    assert calls == [("systemctl", "--user", "restart", "litellm-proxy")]


@pytest.mark.asyncio
async def test_restart_litellm_failure_502s(web_app, web_client, monkeypatch):
    class _Proc:
        returncode = 4

        async def wait(self):
            return 4

    async def fake_exec(*args, **kw):
        return _Proc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    app = web_app()
    async with web_client(app) as c:
        r = await c.post("/api/admin/services/restart",
                         json={"service": "litellm-proxy"})
        assert r.status_code == 502


@pytest.mark.asyncio
async def test_restart_self_is_detached_and_delayed(web_app, web_client, monkeypatch):
    popen = []

    class _P:
        def __init__(self, *a, **kw):
            popen.append((a, kw))
    monkeypatch.setattr(subprocess, "Popen", _P)
    app = web_app()
    async with web_client(app) as c:
        r = await c.post("/api/admin/services/restart",
                         json={"service": "jaynet-web"})
        assert r.status_code == 200
        assert "reload" in r.json()["note"]
    (args, kw), = popen
    cmd = args[0][2]
    assert cmd.startswith("sleep 1; ")                # response leaves first
    assert "systemctl --user restart jaynet-web" in cmd
    assert "orchestrator-web" in cmd                   # legacy-unit fallback
    assert kw.get("start_new_session") is True         # survives this process
