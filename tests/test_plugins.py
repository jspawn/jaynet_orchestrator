"""Tests for runtime/plugins.py (loader) and runtime/hooks.py (hook registry)."""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime import hooks, paths, plugins
from runtime.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear()
    yield
    hooks.clear()


def _mk_plugin(root: Path, name: str, manifest: str,
               tool: str | None = None, hook: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(manifest, encoding="utf-8")
    if tool:
        tdir = d / "tools" / "ns"
        tdir.mkdir(parents=True)
        (tdir / "thing.py").write_text(tool, encoding="utf-8")
    if hook:
        (d / "hooks.py").write_text(hook, encoding="utf-8")
    return d


@pytest.fixture
def layers(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    installed = tmp_path / "installed"
    builtin.mkdir()
    installed.mkdir()
    monkeypatch.setattr(paths, "PLUGINS_BUILTIN_DIR", builtin)
    monkeypatch.setattr(paths, "PLUGINS_DIR", installed)
    return builtin, installed


_TOOL_SRC = '''
from runtime.tool_base import Tool, ToolContext, ToolResult

class NsThing(Tool):
    name = "ns.thing"
    description = "test tool"
    parameters = {"type": "object", "properties": {}, "required": []}
    async def execute(self, args, ctx):
        return ToolResult(status="ok", result={})
'''

_HOOK_SRC = '''
CALLS = []

def on_project_delete(owner, pid):
    CALLS.append((owner, pid))
'''


def test_builtin_defaults_disabled_installed_enabled(layers):
    builtin, installed = layers
    _mk_plugin(builtin, "alpha", "name: alpha\nversion: '1.0'\n")
    _mk_plugin(installed, "beta", "name: beta\nversion: '1.0'\n")
    infos = {i.name: i for i in plugins.scan({})}
    assert infos["alpha"].enabled is False and infos["alpha"].state == "disabled"
    assert infos["beta"].enabled is True and infos["beta"].state == "loaded"
    assert infos["alpha"].origin == "builtin"
    assert infos["beta"].origin == "installed"


def test_config_override_flips_enabled(layers):
    builtin, _ = layers
    _mk_plugin(builtin, "alpha", "name: alpha\n")
    cfg = {"plugins": {"alpha": {"enabled": True}}}
    (info,) = plugins.scan(cfg)
    assert info.enabled is True and info.state == "loaded"


def test_missing_dependency_marks_unavailable(layers):
    _, installed = layers
    _mk_plugin(installed, "beta",
               "name: beta\ndependencies: [no_such_module_xyz]\n")
    (info,) = plugins.scan({})
    assert info.state == "unavailable"
    assert info.missing == ["no_such_module_xyz"]
    assert "no_such_module_xyz" in info.reason


def test_requires_jaynet_gate(layers):
    _, installed = layers
    _mk_plugin(installed, "beta", "name: beta\nrequires_jaynet: '>=999.0.0'\n")
    (info,) = plugins.scan({})
    assert info.state == "unavailable"
    assert "999" in info.reason


def test_installed_shadows_builtin(layers):
    builtin, installed = layers
    _mk_plugin(builtin, "alpha", "name: alpha\nversion: '1.0'\n")
    _mk_plugin(installed, "alpha", "name: alpha\nversion: '2.0'\n")
    infos = plugins.scan({})
    assert len(infos) == 1
    assert infos[0].origin == "installed" and infos[0].version == "2.0"


def test_load_registers_tools_and_hooks(layers):
    _, installed = layers
    _mk_plugin(installed, "beta", "name: beta\n", tool=_TOOL_SRC, hook=_HOOK_SRC)
    reg = ToolRegistry(layers[1])          # any dir; discover() not needed
    infos = plugins.load({}, reg)
    assert infos[0].state == "loaded"
    assert reg.get("ns.thing") is not None
    assert infos[0].hooks == ["on_project_delete"]
    hooks.fire("on_project_delete", "u", "p1")   # must not raise


def test_disabled_plugin_never_imported(layers):
    builtin, _ = layers
    _mk_plugin(builtin, "alpha", "name: alpha\n", tool=_TOOL_SRC, hook=_HOOK_SRC)
    reg = ToolRegistry(builtin)
    plugins.load({}, reg)
    assert reg.get("ns.thing") is None
    assert hooks.registered("on_project_delete") == []


def test_hook_fire_isolates_exceptions():
    def boom(owner, pid):
        raise RuntimeError("bad plugin")
    seen = []
    hooks.register("on_project_delete", boom)
    hooks.register("on_project_delete", lambda o, p: seen.append(p))
    out = hooks.fire("on_project_delete", "u", "p1")
    assert seen == ["p1"]
    assert out == []   # lambda returned None (append)


def test_unknown_hook_refused():
    assert hooks.register("no_such_hook", lambda: None) is False


def test_routes_module_none_when_not_loaded(layers):
    builtin, _ = layers
    _mk_plugin(builtin, "alpha", "name: alpha\n")
    (info,) = plugins.scan({})
    assert plugins.routes_module(info) is None


def test_skill_dirs_only_for_loaded(layers, tmp_path):
    builtin, installed = layers
    d = _mk_plugin(installed, "beta", "name: beta\n")
    (d / "skills" / "s").mkdir(parents=True)
    (d / "skills" / "s" / "SKILL.md").write_text("---\nname: s\n---\nx\n")
    infos = plugins.scan({})
    dirs = plugins.skill_dirs(infos)
    assert list(dirs) == ["beta"]


def test_version_tuple_compare():
    assert plugins._version_ok(">=1.1.0", "1.1.0")
    assert plugins._version_ok(">=1.1", "1.1.0")
    assert plugins._version_ok(">=1.2.0", "1.10.0") is not False  # 1.10 >= 1.2
    assert plugins._version_ok(">=1.10.0", "1.10.0")
    assert not plugins._version_ok(">=2.0.0", "1.9.9")
    assert plugins._version_ok("", "0.0.1")
    assert plugins._version_ok("garbage", "0.0.1")
