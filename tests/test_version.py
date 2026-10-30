"""Version single-source-of-truth: runtime.__version__ surfaces in
/api/health (unauthenticated) and /api/admin/status."""
from pathlib import Path

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


@pytest.mark.asyncio
async def test_admin_status_exposes_fallbacks_without_eval_tag(web_app, web_client):
    app = web_app()
    app.state.runtime._served_model_log = [{
        "ts": 123.0, "requested": "local-specialist",
        "served": "local-orchestrator", "tag": "internal-eval-tag",
    }]
    async with web_client(app) as c:
        r = await c.get("/api/admin/status")
    assert r.status_code == 200
    assert r.json()["model_fallbacks"] == [{
        "ts": 123.0, "requested": "local-specialist",
        "served": "local-orchestrator",
    }]


def test_admin_overview_renders_fallback_warning():
    root = Path(__file__).parent.parent
    html = (root / "web/static/admin.html").read_text()
    js = (root / "web/static/admin.js").read_text()
    assert 'id="fallbacks"' in html
    assert "d.model_fallbacks" in js
    assert "Model fallback detected" in js
    assert '/static/admin.js?v=16' in html
