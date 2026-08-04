"""Version single-source-of-truth: runtime.__version__ surfaces in
/api/health (unauthenticated) and /api/admin/status."""
import httpx
import pytest

import runtime


@pytest.mark.asyncio
async def test_health_reports_version(web_app):
    app = web_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/health")
        assert r.status_code == 200
        assert r.json()["version"] == runtime.__version__


@pytest.mark.asyncio
async def test_admin_status_reports_version(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/status")
        assert r.status_code == 200
        assert r.json()["process"]["version"] == runtime.__version__
