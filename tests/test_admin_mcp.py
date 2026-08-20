"""Admin → Tools → MCP servers: CRUD endpoints persist tools.mcp.servers as a
config override and apply it live; validation rejects malformed entries."""

import pytest


@pytest.mark.asyncio
async def test_mcp_servers_empty_by_default(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/mcp-servers")
        assert r.status_code == 200
        d = r.json()
        assert d["servers"] == {}
        assert "available" in d and "timeout_s" in d


@pytest.mark.asyncio
async def test_mcp_servers_put_roundtrip_and_live(web_app, web_client):
    app = web_app()
    servers = {
        "fs": {"command": "npx",
               "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
               "env": {"FOO": "bar"}, "confirm": True},
        "api": {"url": "http://192.168.1.10:8000/mcp", "confirm": False},
    }
    async with web_client(app) as c:
        r = await c.put("/api/admin/mcp-servers", json={"servers": servers})
        assert r.status_code == 200
        assert r.json()["servers"] == ["api", "fs"]
        r = await c.get("/api/admin/mcp-servers")
        got = r.json()["servers"]
        assert got["fs"]["command"] == "npx"
        assert got["fs"]["confirm"] is True
        assert got["api"]["confirm"] is False
        # Applied live: the mcp client sees them without a restart.
        from tools.mcp import client as mcp_client
        assert set(mcp_client.servers(app.state.runtime.config)) == {"fs", "api"}
        # Persisted as a config override (survives restarts).
        overrides = app.state.users.get_config_overrides()
        assert set(overrides["tools.mcp.servers"]) == {"fs", "api"}
        # Clearing drops the override again.
        r = await c.put("/api/admin/mcp-servers", json={"servers": {}})
        assert r.status_code == 200
        assert "tools.mcp.servers" not in app.state.users.get_config_overrides()


@pytest.mark.asyncio
async def test_mcp_servers_validation(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        for bad, frag in (
            ({"Bad Name!": {"url": "http://x"}}, "invalid server name"),
            ({"ok": {}}, "needs either"),
            ({"ok": "string"}, "must be an object"),
            ({"ok": {"command": "npx", "args": "notalist"}}, "args must be a list"),
            ({"ok": {"command": "npx", "env": ["x"]}}, "env must be an object"),
        ):
            r = await c.put("/api/admin/mcp-servers", json={"servers": bad})
            assert r.status_code == 400, bad
            assert frag in r.json()["detail"]
        r = await c.put("/api/admin/mcp-servers", json={"servers": ["x"]})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_mcp_servers_test_endpoint(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        # Unknown server → 404.
        r = await c.post("/api/admin/mcp-servers/test", json={"name": "ghost"})
        assert r.status_code == 404
        # Configured but unreachable → ok:false with an actionable error
        # (no crash; the mcp package may not even be installed).
        await c.put("/api/admin/mcp-servers",
                    json={"servers": {"x": {"command": "no-such-binary-xyz"}}})
        r = await c.post("/api/admin/mcp-servers/test", json={"name": "x"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False and d["error"]
