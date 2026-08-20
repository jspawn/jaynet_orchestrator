"""Plugin loader — optional capability bundles that extend JayNet by choice.

A plugin is a directory with a `plugin.yaml` manifest:

    plugins/graphify/
      plugin.yaml     # name, version, description, requires_jaynet, dependencies[]
      tools/          # optional: <ns>/<verb>.py Tool subclasses (discover_extra-style)
      skills/         # optional: <name>/SKILL.md skill layer (origin "plugin:<name>")
      hooks.py        # optional: functions named after runtime.hooks.HOOK_NAMES
      routes.py       # optional: register(app, state) — same contract as web/routes_*

Two layers, same split as skills (runtime/paths.py):

  - builtin   $JAYNET_HOME/plugins   versioned with the repo, default DISABLED
                                     (opt-in: they usually need extra pip deps)
  - installed $JAYNET_DATA/plugins   admin-installed, survives git pulls,
                                     default enabled

An installed plugin with the same name overrides the builtin one.

Enabled state lives in runtime.yaml: `plugins.<name>.enabled`. Disabled =
never imported, so a broken plugin can never take JayNet down. Declared pip
dependencies are checked via importlib.util.find_spec before import; missing
deps mark the plugin `unavailable` (with the list) instead of failing.

Trust model: plugins are admin-installed Python running in-process — full
trust, same as DATA/custom tools. Only install code you audited. Plugins see
core only through runtime.hooks and the documented register(app, state)
contract; they must never import web/* or mutate core state.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from runtime import __version__, hooks, paths

log = logging.getLogger(__name__)


@dataclass
class PluginInfo:
    """Status of one discovered plugin, for the admin API and the loader."""

    name: str
    version: str
    description: str
    origin: str                       # "builtin" | "installed"
    dir: Path
    enabled: bool
    state: str                        # "loaded" | "disabled" | "unavailable"
    missing: list[str] = field(default_factory=list)   # unmet import names
    reason: str = ""                  # human-readable state detail
    hooks: list[str] = field(default_factory=list)     # hook names it registered
    has_tools: bool = False
    has_skills: bool = False
    has_routes: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version,
            "description": self.description, "origin": self.origin,
            "enabled": self.enabled, "state": self.state,
            "missing": self.missing, "reason": self.reason,
            "hooks": self.hooks, "has_tools": self.has_tools,
            "has_skills": self.has_skills, "has_routes": self.has_routes,
        }


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in str(v).split(".") if p.isdigit())
    except ValueError:
        return ()


def _version_ok(requirement: str, current: str = __version__) -> bool:
    """Only '>=' comparisons are supported (e.g. ">=1.1.0"); anything else
    (or empty) passes. Pre-release suffixes are ignored."""
    req = (requirement or "").strip()
    if not req.startswith(">="):
        return True
    want, have = _version_tuple(req[2:].strip()), _version_tuple(current)
    if not want or not have:
        return True
    n = max(len(want), len(have))
    return have + (0,) * (n - len(have)) >= want + (0,) * (n - len(want))


def _read_manifest(plugin_dir: Path) -> dict[str, Any] | None:
    mf = plugin_dir / "plugin.yaml"
    if not mf.is_file():
        return None
    try:
        data = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.error("Unreadable manifest %s: %s", mf, e)
        return None
    return data if isinstance(data, dict) else None


def _discover_dirs() -> dict[str, tuple[Path, str]]:
    """{name: (dir, origin)} over both layers; installed overrides builtin."""
    found: dict[str, tuple[Path, str]] = {}
    for root, origin in ((paths.PLUGINS_BUILTIN_DIR, "builtin"),
                         (paths.PLUGINS_DIR, "installed")):
        if not root.is_dir():
            continue
        for sub in sorted(p for p in root.iterdir() if p.is_dir()):
            mf = _read_manifest(sub)
            if mf is None:
                continue
            name = str(mf.get("name") or sub.name)
            found[name] = (sub, origin)
    return found


def _import_file(mod_name: str, path: Path):
    """Import one .py file from a non-package dir (plugin code is not a
    package); mirrors registry.discover_extra's isolation approach."""
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def scan(config: dict[str, Any]) -> list[PluginInfo]:
    """Discover all plugins and compute their status WITHOUT importing any
    plugin code (safe at any time — used by the admin API)."""
    plugins_cfg = (config or {}).get("plugins") or {}
    if not isinstance(plugins_cfg, dict):
        log.error("plugins: config section is not a mapping — ignoring it")
        plugins_cfg = {}
    infos: list[PluginInfo] = []
    for name, (plugin_dir, origin) in _discover_dirs().items():
        mf = _read_manifest(plugin_dir) or {}
        default_enabled = origin == "installed"
        # A hand-edited `plugins.<name>:` (YAML null) or scalar must degrade
        # to defaults, never crash boot.
        pcfg = plugins_cfg.get(name)
        enabled = bool((pcfg if isinstance(pcfg, dict) else {})
                       .get("enabled", default_enabled))
        info = PluginInfo(
            name=name,
            version=str(mf.get("version") or "?"),
            description=str(mf.get("description") or ""),
            origin=origin, dir=plugin_dir, enabled=enabled,
            state="disabled" if not enabled else "loaded",
            has_tools=(plugin_dir / "tools").is_dir(),
            has_skills=(plugin_dir / "skills").is_dir(),
            has_routes=(plugin_dir / "routes.py").is_file(),
        )
        if enabled:
            if not _version_ok(str(mf.get("requires_jaynet") or "")):
                info.state, info.reason = "unavailable", (
                    f"requires JayNet {mf['requires_jaynet']} (running {__version__})")
            else:
                # find_spec can raise on malformed dep names (bad dot paths,
                # missing parent packages) — treat as "missing", never crash.
                missing: list[str] = []
                for d in mf.get("dependencies") or []:
                    try:
                        if importlib.util.find_spec(str(d)) is None:
                            missing.append(str(d))
                    except Exception:
                        missing.append(str(d))
                info.missing = missing
                if info.missing:
                    info.state = "unavailable"
                    info.reason = "missing dependencies: " + ", ".join(info.missing)
        infos.append(info)
    return sorted(infos, key=lambda i: i.name)


def load(config: dict[str, Any], registry) -> list[PluginInfo]:
    """Import every enabled+available plugin: register its tools (via
    `registry.discover_extra`) and hooks. Returns the scan() list with
    `hooks` filled in for loaded plugins. Routes/skills are consumed by the
    web layer via routes_module()/skill_dirs() below."""
    infos = scan(config)
    for info in infos:
        if info.state != "loaded":
            continue
        if info.has_tools:
            try:
                registry.discover_extra(info.dir / "tools")
            except Exception as e:
                log.error("Plugin %s tools failed to load: %s", info.name, e)
        hooks_file = info.dir / "hooks.py"
        if hooks_file.is_file():
            try:
                mod = _import_file(f"jaynet_plugin_{info.name}_hooks", hooks_file)
                for name in hooks.HOOK_NAMES:
                    fn = getattr(mod, name, None)
                    if callable(fn) and hooks.register(name, fn):
                        info.hooks.append(name)
            except Exception as e:
                log.error("Plugin %s hooks failed to load: %s", info.name, e)
        if info.hooks or info.has_tools:
            log.info("Plugin loaded: %s %s (hooks=%s, tools=%s)",
                     info.name, info.version, info.hooks, info.has_tools)
        if info.has_skills:
            from runtime import skills as skills_mod
            skills_mod.register_plugin_skills(info.name, info.dir / "skills")
    return infos


def routes_module(info: PluginInfo):
    """The plugin's routes.py as a module, or None. Called by the web layer
    after core routes are registered."""
    if info.state != "loaded" or not info.has_routes:
        return None
    try:
        return _import_file(f"jaynet_plugin_{info.name}_routes", info.dir / "routes.py")
    except Exception as e:
        log.error("Plugin %s routes failed to load: %s", info.name, e)
        return None


def skill_dirs(infos: list[PluginInfo]) -> dict[str, Path]:
    """{plugin_name: skills_dir} for loaded plugins with a skills layer."""
    return {i.name: i.dir / "skills" for i in infos
            if i.state == "loaded" and i.has_skills}
