"""Tests for the custom Python tool layer (registry.discover_extra /
register_instance). Custom tools live as bare *.py files in
ORCH_DATA/custom/tools — loaded by file path, never crashing discovery."""
from __future__ import annotations

from conftest import run

from runtime.registry import ToolRegistry
from runtime.tool_base import ToolContext

VALID = '''
from runtime.tool_base import Tool, ToolContext, ToolResult


class Ping(Tool):
    name = "custom.ping"
    description = "custom ping tool written by the admin"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args, ctx):
        return ToolResult(status="ok", result="pong")
'''

BROKEN = "def nope(:\n"


def _registry(tmp_path):
    """A registry whose (empty) builtin root discovers nothing."""
    root = tmp_path / "builtin-tools"
    root.mkdir()
    reg = ToolRegistry(root)
    reg.discover()
    return reg


def test_valid_custom_tool_registers_and_runs(tmp_path):
    d = tmp_path / "custom"
    (d / "custom").mkdir(parents=True)
    (d / "custom" / "ping.py").write_text(VALID)
    reg = _registry(tmp_path)
    reg.discover_extra(d)
    tool = reg.get("custom.ping")
    assert tool is not None
    ctx = ToolContext(request_id="t", config={}, budget=None)
    res = run(tool.execute({}, ctx))
    assert res.status == "ok"
    assert res.result == "pong"


def test_broken_file_is_skipped(tmp_path, caplog):
    d = tmp_path / "custom"
    d.mkdir()
    (d / "broken.py").write_text(BROKEN)
    (d / "ping.py").write_text(VALID)
    reg = _registry(tmp_path)
    with caplog.at_level("ERROR"):
        reg.discover_extra(d)
    assert reg.get("custom.ping") is not None      # the good one still loads
    assert any("broken.py" in r.getMessage() for r in caplog.records)


def test_underscore_files_ignored(tmp_path):
    d = tmp_path / "custom"
    d.mkdir()
    (d / "_draft.py").write_text(VALID.replace("custom.ping", "custom.draft"))
    reg = _registry(tmp_path)
    reg.discover_extra(d)
    assert reg.get("custom.draft") is None


def test_name_collision_with_existing_tool_refused(tmp_path, caplog):
    d = tmp_path / "custom"
    d.mkdir()
    (d / "ping.py").write_text(VALID.replace("custom.ping", "skill.load"))
    reg = _registry(tmp_path)

    from runtime.tool_base import Tool
    builtin = type("SkillLoad", (Tool,), {
        "name": "skill.load",
        "description": "the builtin one",
        "execute": lambda self, a, c: None,
    })()
    assert reg.register_instance(builtin)
    with caplog.at_level("WARNING"):
        reg.discover_extra(d)
    assert reg.get("skill.load") is builtin        # not replaced
    assert any("collides" in r.getMessage() for r in caplog.records)


def test_register_instance_refuses_overwrite(tmp_path, caplog):
    reg = _registry(tmp_path)

    from runtime.tool_base import Tool
    def mk(name):
        return type("T", (Tool,), {
            "name": name, "description": "x" * 30,
            "execute": lambda self, a, c: None})()

    first, second = mk("custom.a"), mk("custom.a")
    assert reg.register_instance(first) is True
    with caplog.at_level("WARNING"):
        assert reg.register_instance(second) is False
    assert reg.get("custom.a") is first
