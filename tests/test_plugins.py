"""Tests for runtime/plugins.py (loader) and runtime/hooks.py (hook registry)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from runtime import hooks, paths, plugins
from runtime.loop import AgentRuntime
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


def test_scan_survives_malformed_plugins_config(layers):
    """Hand-edited YAML foot-guns (None / scalar / non-dict section) must
    degrade to defaults, never raise — scan() runs on the boot path."""
    builtin, _ = layers
    _mk_plugin(builtin, "alpha", "name: alpha\n")
    for bad in ({"alpha": None}, {"alpha": 5}):
        (info,) = plugins.scan({"plugins": bad})
        assert info.enabled is False
    (info,) = plugins.scan({"plugins": "oops"})
    assert info.enabled is False


def test_malformed_dependency_name_marks_unavailable(layers):
    """find_spec raises on malformed names — treated as missing, not fatal."""
    _, installed = layers
    _mk_plugin(installed, "beta",
               "name: beta\ndependencies: ['..bad..name']\n")
    (info,) = plugins.scan({})
    assert info.state == "unavailable"
    assert info.missing == ["..bad..name"]


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
    assert plugins._version_ok(">=1.2.0", "1.10.0") is True  # 1.10 >= 1.2
    assert plugins._version_ok(">=1.10.0", "1.10.0")
    assert not plugins._version_ok(">=2.0.0", "1.9.9")
    assert plugins._version_ok("", "0.0.1")
    assert plugins._version_ok("garbage", "0.0.1")


def test_agentruntime_overrides_merge_before_plugin_load(layers, tmp_path):
    """Regression (live-confirmed): admin-persisted config overrides must
    merge BEFORE plugins.load — a plugin toggled in Admin used to report
    'loaded' in the Plugins tab while its tools never registered, because
    AgentRuntime loaded plugins from the YAML-only config and the web layer
    applied the overrides afterwards."""
    builtin, _ = layers
    _mk_plugin(builtin, "alpha", "name: alpha\n", tool=_TOOL_SRC)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "sys.md").write_text("SYS")
    cfg = {
        "orchestrator": {"litellm_base": "http://127.0.0.1:1",
                         "model": "local-orchestrator",
                         "system_prompt": "prompts/sys.md"},
        "trace": {"db_path": str(tmp_path / "trace.db"), "log_content": False},
        "costs": {},
        "budgets": {},
    }
    cdir = tmp_path / "config"
    cdir.mkdir()
    (cdir / "runtime.yaml").write_text(yaml.safe_dump(cfg))

    rt = AgentRuntime(cdir / "runtime.yaml",
                      config_overrides={"plugins.alpha.enabled": True})
    assert "ns.thing" in {t.name for t in rt.registry.all()}
    assert any(p.name == "alpha" and p.state == "loaded" for p in rt.plugins)
    assert rt.config["plugins"]["alpha"]["enabled"] is True

    # Control: the same YAML without overrides keeps the plugin disabled.
    hooks.clear()
    rt2 = AgentRuntime(cdir / "runtime.yaml")
    assert "ns.thing" not in {t.name for t in rt2.registry.all()}
    assert all(p.state != "loaded" for p in rt2.plugins)


def test_early_users_store_resolves_relative_paths(tmp_path, monkeypatch):
    """The early users-DB lookup must anchor relative config paths exactly
    like the runtime does (load_config -> ORCH_DATA). A raw yaml.safe_load
    opened 'users.db' next to the process CWD instead — a stray empty DB
    with zero overrides, so the plugin toggle still did nothing after the
    ordering fix (live-confirmed follow-up)."""
    data = tmp_path / "data"
    monkeypatch.setattr(paths, "DATA", data)
    cfgdir = tmp_path / "cfg"
    cfgdir.mkdir()
    (cfgdir / "runtime.yaml").write_text(
        "web:\n  users_db: users.db\n  chats_db: chats.db\n")
    from web.server import _early_users_store
    store = _early_users_store(str(cfgdir / "runtime.yaml"))
    assert store.db_path == str(data / "users.db")


def test_scan_reports_ui_bins_readme_and_dependencies(layers):
    """The Plugins-tab discovery data: has_ui, declared pip deps, missing
    executables (requires_bins — reported, never blocking) and the README."""
    _, installed = layers
    d = _mk_plugin(installed, "uiplug", """
name: uiplug
version: "1.0"
description: ui test
dependencies: [yaml]
requires_bins: [definitely-not-a-real-binary-xyz, sh]
""")
    (d / "ui").mkdir()
    (d / "ui" / "index.html").write_text("<html></html>")
    (d / "README.md").write_text("install notes here")
    info = {i.name: i for i in plugins.scan({})}["uiplug"]
    assert info.has_ui is True
    assert info.dependencies == ["yaml"]
    # the fake binary is reported missing, sh (always present) is not —
    # and state stays "loaded": missing bins degrade features, never block
    assert info.missing_bins == ["definitely-not-a-real-binary-xyz"]
    assert info.state == "loaded"
    assert "install notes" in info.readme
    d2 = info.as_dict()
    for key in ("has_ui", "dependencies", "missing_bins", "readme"):
        assert key in d2


def test_scan_no_ui_no_readme_defaults(layers):
    _, installed = layers
    _mk_plugin(installed, "plain", "name: plain\nversion: '1.0'\n")
    info = {i.name: i for i in plugins.scan({})}["plain"]
    assert info.has_ui is False
    assert info.missing_bins == []
    assert info.readme == ""
