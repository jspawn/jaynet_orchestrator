"""Shared fixtures for the tool test suite.

Self-contained: every test gets a throwaway directory that is registered as the
sole fs/git allowed root, plus a ToolContext factory. No network, no real GPUs,
no system mutation. Run with the orchestrator venv:

    /srv/orchestrator/.venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

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
