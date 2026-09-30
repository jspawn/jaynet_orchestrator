"""jev lifecycle — manage the jevify sidecar as a JayNet process (no HTTP
routes; same pattern as plugins/imagegen/routes.py).

With plugins.jev.manage_sidecar: true and plugins.jev.recipe pointing at a
probed jevify recipe, register() adds `jevify serve <recipe> --port <base_url
port>` to the process manager: the sidecar shows up in admin → Processes with
status/start/stop/restart/logs like any model slot, starts at boot, and is
stopped+unregistered at shutdown. Without those two keys nothing happens —
the sidecar stays a manual/self-managed process (plugins/jev/README.md).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_NAME = "jevify"


def register(app, state) -> None:
    if state is None or not hasattr(state, "startup_hooks"):
        return
    from runtime import process_manager
    mgr = process_manager.CURRENT
    if mgr is None:
        return
    runtime = getattr(state, "runtime", None)
    cfg = (((runtime.config if runtime else {}) or {}).get("plugins") or {}) \
        .get("jev") or {}
    if not (cfg.get("enabled") and cfg.get("manage_sidecar")):
        return
    recipe = (cfg.get("recipe") or "").strip()
    binary = shutil.which("jevify")
    if binary is None:
        # uv tool installs land in ~/.local/bin, which a systemd unit's PATH
        # usually lacks — try the default location before giving up.
        cand = Path.home() / ".local" / "bin" / "jevify"
        if cand.exists():
            binary = str(cand)
    if not recipe:
        log.warning("jev manage_sidecar: plugins.jev.recipe is empty — "
                    "sidecar not managed")
        return
    if binary is None:
        log.warning("jev manage_sidecar: 'jevify' not on PATH "
                    "(uv tool install jevify) — sidecar not managed")
        return
    port = urlparse(cfg.get("base_url") or "").port or 8600
    mgr.add(_NAME, f"{binary} serve {recipe} --port {port}",
            restart=True, restart_delay=10, kill_signal=15)

    async def _jevify_start() -> None:
        await mgr.start_one(_NAME)

    async def _jevify_stop() -> None:
        await mgr.remove(_NAME)

    state.startup_hooks.append(_jevify_start)
    state.shutdown_hooks.append(_jevify_stop)

    # Hot-enable path: boot already happened, so the startup hook never
    # fires — start now. At boot there is no running loop yet; the startup
    # hook covers that case. Hot-disable is covered too: the admin toggle
    # awaits the recorded shutdown hooks BEFORE disable_live unregisters
    # them (web/routes_plugins.py) — the sidecar stops+unregisters with
    # the toggle, not at the next service shutdown.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    from runtime.proc import spawn_background
    spawn_background(mgr.start_one(_NAME), name="jevify-sidecar-start")
