"""Shared fixtures for the tool test suite.

Self-contained: every test gets a throwaway directory that is registered as the
sole fs/git allowed root, plus a ToolContext factory. No network, no real GPUs,
no system mutation. Run from the checkout root with its own venv:

    .venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml

# Pin the data-dir default BEFORE any runtime import computes the paths.py
# constants: without this, any test that falls through to a default path
# (e.g. a wrong config key) writes into /srv/orchestrator/data and
# resurrects the deleted live-install tree. Same family as the
# ORCH_LITELLM_CONFIG pin in web_app below.
_TEST_DATA = tempfile.mkdtemp(prefix="jaynet-test-data-")
os.environ.setdefault("JAYNET_DATA", _TEST_DATA)
os.environ.setdefault("ORCH_DATA", _TEST_DATA)
# HOME gets the same treatment, anchored at the checkout: on CI runners
# ORCH_HOME is unset and paths.HOME would fall back to /srv/orchestrator,
# which doesn't exist there (the fj-test CI failure). Locally an explicit
# ORCH_HOME wins via setdefault.
os.environ.setdefault("JAYNET_HOME", str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ORCH_HOME", str(Path(__file__).resolve().parent.parent))

# One temp dir per suite run would otherwise accumulate in /tmp.
import atexit  # noqa: E402
import shutil  # noqa: E402

atexit.register(shutil.rmtree, _TEST_DATA, True)

from runtime.tool_base import ToolContext


def run(coro):
    """Run a coroutine to completion (avoids pytest-asyncio mode coupling)."""
    return asyncio.new_event_loop().run_until_complete(coro)


# --- Guard: never create directories under the paths.py literal default ------
# The env pins above make paths.HOME/DATA safe, but a test can still bypass
# them (recomputed paths, a subprocess without the env, a hardcoded path).
# /srv/orchestrator doesn't exist on CI and IS a live install on dev boxes —
# creating directories there either fails CI or pollutes live data. Tests
# must anchor at tmp_path or the checkout. Comparison-only reads of
# paths.HOME/DATA are unaffected.
_DEFAULT_ROOT = Path("/srv/orchestrator").resolve()


@pytest.fixture(autouse=True)
def _guard_default_home_writes(monkeypatch):
    real_mkdir = Path.mkdir
    real_makedirs = os.makedirs

    def _blocked(target) -> bool:
        t = Path(target).resolve()
        return t == _DEFAULT_ROOT or _DEFAULT_ROOT in t.parents

    def _refuse(target):
        raise RuntimeError(
            f"test tried to create {target} under the paths.py default root "
            "/srv/orchestrator — anchor at tmp_path or the checkout root "
            "(on dev boxes that path is a live install)")

    def guarded_mkdir(self, *a, **k):
        if _blocked(self):
            _refuse(self)
        return real_mkdir(self, *a, **k)

    def guarded_makedirs(name, *a, **k):
        if _blocked(name):
            _refuse(name)
        return real_makedirs(name, *a, **k)

    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    monkeypatch.setattr(os, "makedirs", guarded_makedirs)


@pytest.fixture(autouse=True)
def _guard_real_systemctl(monkeypatch):
    """systemd user units are global to the user — ORCH_HOME/tmp configs do
    NOT namespace them. A test app code path that shells out to systemctl
    (e.g. _reload_proxy's restart fallback after a cloud-catalog PUT)
    restarts the LIVE services on a dev box; one suite run killed a 14-hour
    eval this way. Fake systemctl everywhere. Tests asserting specific calls
    monkeypatch the subprocess factory themselves (applied later → they win).
    """
    real_exec = asyncio.create_subprocess_exec
    real_popen = subprocess.Popen

    class _FakeProc:
        returncode = 0

        async def wait(self):
            return 0

    async def _exec(*args, **kw):
        if args and str(args[0]) == "systemctl":
            return _FakeProc()
        return await real_exec(*args, **kw)

    class _FakePopen:
        def __init__(self, *a, **kw):
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    def _popen(*args, **kw):
        if args and "systemctl" in str(args[0]):
            return _FakePopen()
        return real_popen(*args, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(subprocess, "Popen", _popen)


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
        # Preset seed paths point at the live install (/srv/orchestrator) or
        # are ORCH_HOME-relative; tests must exercise THIS checkout's files.
        for p in (cfg["models"].get("presets") or {}).values():
            src = p.get("preset") or ""
            if src.startswith("/srv/orchestrator/"):
                p["preset"] = str(WEB_ROOT / src.removeprefix("/srv/orchestrator/"))
            elif src and not src.startswith("/"):
                p["preset"] = str(WEB_ROOT / src)
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
        # cloud-model seed defaults to $ORCH_HOME/config/litellm.yaml — pin it
        # to THIS checkout so tests don't depend on whatever ORCH_HOME is
        monkeypatch.setenv("ORCH_LITELLM_CONFIG",
                           str(WEB_ROOT / "config" / "litellm.yaml"))
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
