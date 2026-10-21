"""Scoped /selftest: parser, no-model help, and agent-run web wiring."""

import asyncio
from pathlib import Path

import pytest

from runtime.selftest import parse_command


class _Tool:
    def __init__(self, name, hidden=False):
        self.name = name
        self.hidden = hidden
        self.description = f"about {name}"
        self.private = False
        self.requires_confirmation = False
        self.parameters = {}


class _Reg:
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}

    def all(self):
        return list(self._tools.values())

    def get(self, name):
        return self._tools.get(name)


TOOLS = [
    _Tool("skill.list"), _Tool("skill.load"), _Tool("trace.coverage"),
    _Tool("fs.read"), _Tool("code.check"), _Tool("code.old", hidden=True),
]
SKILLS = {"coding": {"description": "code"}, "pdf": {"description": "PDF"}}


def _parse(text):
    return parse_command(text, _Reg(TOOLS), SKILLS)


def test_parse_bare_shows_help_and_other_input_is_untouched():
    assert _parse("/selftest")["action"] == "help"
    assert _parse("/selftest help")["action"] == "help"
    assert "does not start a test" in _parse("/selftest")["text"]
    assert _parse("test all tools") is None
    assert _parse("/selftester all") is None


def test_parse_all_and_tools_use_advertised_toolset_only():
    all_spec = _parse("/selftest all")
    assert all_spec["action"] == "run"
    assert "ALL advertised tools and ALL skills" in all_spec["message"]
    assert "code.old" not in all_spec["tools"]
    assert set(all_spec["tools"]) == {
        "skill.list", "skill.load", "trace.coverage", "fs.read", "code.check"}
    assert "selftest" in all_spec["extra_system"]

    tools_spec = _parse("/selftest tools")
    assert "not the skill catalog" in tools_spec["message"]
    assert tools_spec["tools"] == all_spec["tools"]


def test_parse_skills_and_exact_targets_narrow_allowlist():
    skills = _parse("/selftest skills")
    assert skills["tools"] == ["skill.list", "skill.load", "trace.coverage"]
    assert "EVERY skill" in skills["message"]

    one = _parse("/selftest fs.read")
    assert one["tools"] == ["skill.load", "trace.coverage", "fs.read"]
    assert "ONLY these tools: `fs.read`" in one["message"]

    mixed = _parse("/selftest fs.read, coding pdf")
    assert mixed["tools"] == [
        "skill.list", "skill.load", "trace.coverage", "fs.read"]
    assert "ONLY these tools: `fs.read`" in mixed["message"]
    assert "ONLY these skills: `coding`, `pdf`" in mixed["message"]


def test_parse_rejects_unknown_or_reserved_mixed_names():
    assert _parse("/selftest no.such")["action"] == "error"
    assert "unknown tool/skill" in _parse("/selftest no.such")["text"]
    assert _parse("/selftest code.old")["action"] == "error"
    assert _parse("/selftest tools fs.read")["action"] == "error"


def _install_catalog(app):
    app.state.runtime.registry._tools = {t.name: t for t in TOOLS}
    app.state.runtime.skills = SKILLS


def _spy_run(app, seen):
    async def fake_run(message, **kw):
        seen["message"] = message
        seen["tools"] = kw.get("tools")
        seen["extra_system"] = kw.get("extra_system")
        seen["auto_confirm"] = kw.get("auto_confirm")
        await kw["on_event"]({"type": "run_finish", "seq": 1})
        return {"answer": "ok"}
    app.state.runtime.run = fake_run


@pytest.mark.asyncio
async def test_web_bare_selftest_returns_help_without_agent(web_app, web_client):
    app = web_app(stub_run=False)
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "/selftest"})
        rid = r.json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert not seen
    assert "/selftest all" in r.text and "does not start a test" in r.text


@pytest.mark.asyncio
async def test_web_scoped_selftest_becomes_agent_run(web_app, web_client):
    app = web_app(stub_run=False)
    _install_catalog(app)
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        listed = (await c.get("/api/tools")).json()
        assert [s["name"] for s in listed["skills"]] == ["coding", "pdf"]
        r = await c.post("/api/chat", json={"message": "/selftest skills"})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert seen["tools"] == ["skill.list", "skill.load", "trace.coverage"]
    assert "EVERY skill" in seen["message"]
    assert "trace.coverage" in seen["extra_system"]


@pytest.mark.asyncio
async def test_web_invalid_selftest_is_also_no_model(web_app, web_client):
    app = web_app(stub_run=False)
    _install_catalog(app)
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "/selftest no.such"})
        rid = r.json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert not seen and "unknown tool/skill" in r.text


@pytest.mark.asyncio
async def test_web_selftest_drops_client_auto_confirm(web_app, web_client):
    """Audit 2026-10-21 L3: a self-test run must show every gated side
    effect, so a client-supplied auto-approve never reaches the run."""
    app = web_app(stub_run=False)
    _install_catalog(app)
    seen = {}
    _spy_run(app, seen)
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={
            "message": "/selftest skills", "auto_confirm": True})
        rid = r.json()["run_id"]
        await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert seen and seen["auto_confirm"] is False


def test_composer_has_selftest_meta_and_argument_candidates():
    js = (Path(__file__).parent.parent / "web/static/app.js").read_text()
    assert '{name:"selftest"' in js
    assert 'if(cmd==="selftest")' in js
    assert '(_slashSkills||[])' in js
