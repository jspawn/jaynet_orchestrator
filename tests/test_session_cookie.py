"""Readiness audit QA-1: no test asserted a single session-cookie attribute —
the httponly/samesite flags the no-CSRF-token posture and 51 innerHTML sinks
lean on lived in one keyword argument, and SEC-1's remediation (flipping
web.cookie_secure) had nothing asserting the resulting Secure flag. Starlette
defaults httponly=False, so a one-line slip would have shipped green."""
import httpx
import pytest


async def _login_set_cookie(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as c:
        r = await c.post("/api/login",
                         json={"username": "admin", "password": "pw"})
    assert r.status_code == 200
    return r.headers["set-cookie"]


@pytest.mark.asyncio
async def test_session_cookie_flags_default(web_app):
    cookie = await _login_set_cookie(web_app())
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie          # cookie_secure defaults to false


@pytest.mark.asyncio
async def test_session_cookie_secure_flag_follows_config(web_app):
    """SEC-1 remediation pin: web.cookie_secure: true must put Secure on the
    Set-Cookie header (a nested-key typo silently falls back to false)."""
    cookie = await _login_set_cookie(web_app(web_cfg={"cookie_secure": True}))
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" in cookie
