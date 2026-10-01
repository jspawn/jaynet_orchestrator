"""omnivoice lifecycle hooks — no HTTP routes. Exists so the plugin loader's
register_routes() wires the shutdown hook: a JayNet stop during a keep-warm
window must down tts-server instead of orphaning a GPU-resident process
(same pattern as imagegen, audit #24 D5).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _server_mod():
    """Same load-by-file-path pattern as tools/voice.py (plugin modules are
    not a package)."""
    name = "omnivoice_plugin_server"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parent / "server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


def register(app, state) -> None:
    if state is None or not hasattr(state, "shutdown_hooks"):
        return
    mod = _server_mod()

    async def _omnivoice_shutdown() -> None:
        await mod.SERVER.down()

    state.shutdown_hooks.append(_omnivoice_shutdown)
