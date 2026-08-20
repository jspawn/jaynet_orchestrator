"""Web-layer hook firing: project file changes, project delete, and the
[Project: …] prompt prefix gaining plugin hook text."""

from __future__ import annotations

import asyncio

import pytest

from runtime import hooks


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear()
    yield
    hooks.clear()


@pytest.mark.asyncio
async def test_file_write_and_delete_fire_hooks(web_app, web_client):
    app = web_app()
    changed, deleted = [], []
    hooks.register("on_project_file_changed",
                   lambda o, p, path, pdir: changed.append((o, p, path, pdir)))
    hooks.register("on_project_delete", lambda o, p: deleted.append((o, p)))
    async with web_client(app) as c:
        r = await c.post("/api/projects", json={"name": "Hooked"})
        pid = r.json()["id"]
        r = await c.put(f"/api/projects/{pid}/file?path=a.txt", content=b"hi")
        assert r.status_code == 200
        assert changed[0][:3] == ("admin", pid, "a.txt")
        # The hook receives the RESOLVED projects root, not a re-derived one.
        assert str(changed[0][3]).endswith("projects")
        r = await c.delete(f"/api/projects/{pid}/file?path=a.txt")
        assert r.status_code == 200
        assert changed[-1][:3] == ("admin", pid, "a.txt")
        r = await c.delete(f"/api/projects/{pid}")
        assert r.status_code == 200
        assert deleted == [("admin", pid)]


@pytest.mark.asyncio
async def test_project_prefix_carries_hook_text(web_app, web_client):
    app = web_app(stub_run=False)
    captured = {}

    async def rec(msg, **kw):
        captured["msg"] = msg
        captured.update(kw)
        return {}

    app.state.runtime.run = rec
    hooks.register("augment_project_context",
                   lambda o, p, meta, root: "[TestHook] graph hint line")
    async with web_client(app) as c:
        pid = (await c.post("/api/projects", json={"name": "Prefix"})).json()["id"]
        r = await c.post("/api/chat", json={"message": "hello", "project_id": pid})
        assert r.status_code == 200
        for _ in range(100):
            if captured:
                break
            await asyncio.sleep(0.02)
    assert "[TestHook] graph hint line" in captured["msg"]
    assert "[Project: Prefix]" in captured["msg"]
    # The run wiring threads project_id through to the loop.
    assert captured.get("project_id") == pid


@pytest.mark.asyncio
async def test_throwing_hook_does_not_break_file_write(web_app, web_client):
    app = web_app()

    def boom(o, p, path, pdir):
        raise RuntimeError("bad plugin")

    hooks.register("on_project_file_changed", boom)
    async with web_client(app) as c:
        pid = (await c.post("/api/projects", json={"name": "Boom"})).json()["id"]
        r = await c.put(f"/api/projects/{pid}/file?path=a.txt", content=b"hi")
        assert r.status_code == 200
