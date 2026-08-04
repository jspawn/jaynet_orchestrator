"""Shared fixtures for the tool test suite.

Self-contained: every test gets a throwaway directory that is registered as the
sole fs/git allowed root, plus a ToolContext factory. No network, no real GPUs,
no system mutation. Run with the orchestrator venv:

    /srv/orchestrator/.venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import asyncio
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml

from runtime.tool_base import ToolContext


def run(coro):
    """Run a coroutine to completion (avoids pytest-asyncio mode coupling)."""
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A small Python project tree used as the allowed root."""
    (tmp_path / "app.py").write_text(
        'def greet(name):\n    return "hi " + name\n\n\n'
        'class Widget:\n    def render(self):\n        return greet("world")\n'
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "use.py").write_text("from app import greet\nprint(greet('x'))\n")
    return tmp_path


@pytest.fixture
def config(project: Path) -> dict:
    return {
        "tools": {
            "fs": {"allowed_roots": [str(project)]},
            "git": {"allowed_roots": [str(project)], "default_repo": str(project)},
            "code": {"run": {"default_cwd": str(project), "sandbox_prefix": []}},
            "lint": {},
        },
        "trace": {},
    }


@pytest.fixture
def ctx(config):
    def make(**over):
        cfg = over.pop("config", config)
        return ToolContext(request_id="test", config=cfg, budget=None, **over)
    return make


@pytest.fixture
def git_repo(project: Path) -> Path:
    """Turn the project into a committed git repo."""
    def g(*a):
        subprocess.run(["git", "-C", str(project), *a], check=True,
                       capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(project)], check=True)
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    g("add", "-A"); g("commit", "-qm", "init")
    return project


# ---- in-process web app harness (docs/testing-harness.md) ---------------------
WEB_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def web_app(tmp_path, monkeypatch):
    """Factory for an in-process FastAPI app wired to throwaway dirs.

    web_app()                       the common case: admin/pw session env and
                                    runtime.run stubbed to return {} (no LiteLLM)
    web_app(env={"K": "v"})         extra env vars (e.g. ORCH_WEB_TOKEN)
    web_app(stub_run=False)         leave runtime.run real (tests stub it later)
    """
    def make(env=None, stub_run=True):
        base = tmp_path
        (base / "config").mkdir()
        (base / "prompts").mkdir()
        cfg = yaml.safe_load(open(WEB_ROOT / "config/runtime.yaml"))
        cfg["trace"]["db_path"] = str(base / "trace.db")
        cfg.setdefault("models", {})["presets_db"] = str(base / "presets.db")
        # Preset seed paths point at the live install (/srv/orchestrator);
        # tests must exercise THIS checkout's files instead.
        for p in (cfg["models"].get("presets") or {}).values():
            src = p.get("preset") or ""
            if src.startswith("/srv/orchestrator/"):
                p["preset"] = str(WEB_ROOT / src.removeprefix("/srv/orchestrator/"))
        cfg["orchestrator"]["system_prompt"] = "prompts/orchestrator.md"
        cfg["web"] = {"chats_db": str(base / "chats.db"),
                      "users_db": str(base / "users.db"),
                      "outputs_dir": str(base / "outputs"),
                      "projects_dir": str(base / "projects")}
        (base / "prompts" / "orchestrator.md").write_text("P")
        yaml.safe_dump(cfg, open(base / "config" / "runtime.yaml", "w"))
        monkeypatch.setenv("ORCH_ADMIN_USER", "admin")
        monkeypatch.setenv("ORCH_ADMIN_PASSWORD", "pw")
        monkeypatch.setenv("ORCH_SESSION_SECRET", "t")
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        from web.server import create_app
        app = create_app(str(base / "config" / "runtime.yaml"))
        if stub_run:
            async def fake_run(msg, **kw):   # mock the model — no LiteLLM needed
                return {}
            app.state.runtime.run = fake_run
        return app
    return make


@pytest.fixture
def web_client():
    """Logged-in httpx client over ASGI: `async with web_client(app) as c:`."""
    @asynccontextmanager
    async def client(app, username="admin", password="pw"):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/login", json={"username": username,
                                                 "password": password})
            assert r.status_code == 200
            yield c
    return client


@pytest.fixture
def record_run():
    """Capture the kwargs runtime.run is called with (the run itself is faked)."""
    def make(app):
        seen = {}

        async def rec(msg, **kw):
            seen.update(kw)
            return {}

        app.state.runtime.run = rec
        return seen
    return make
