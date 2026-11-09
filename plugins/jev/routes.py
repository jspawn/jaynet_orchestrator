"""jev lifecycle and preset-driven jevify admin API.

The UI selects an active local preset (unarchived or slotted). Configuration verifies that exact
model is running, probes a temporary jevify recipe, and only then persists the
selection and restarts the managed sidecar. Manual/Open-Jev setups still work;
nothing here loads models or touches remote/cloud presets.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

_NAME = "jevify"
_CONFIGURE_LOCK = asyncio.Lock()


def _load_sibling(module_name: str, filename: str):
    """Load one shared sibling module (plugin files are not a package)."""
    mod = sys.modules.get(module_name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            module_name, Path(__file__).resolve().parent / filename)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
    return mod


def _set_nested(config: dict, dotpath: str, value) -> None:
    parts = dotpath.split(".")
    target = config
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def register(app, state) -> None:
    if state is None or not hasattr(state, "startup_hooks"):
        return
    from runtime import process_manager
    mgr = process_manager.CURRENT
    if mgr is None:
        return
    runtime = getattr(state, "runtime", None)
    if runtime is None:
        return
    cfg = ((runtime.config or {}).get("plugins") or {}).get("jev") or {}
    if not cfg.get("enabled"):
        return
    admin = _load_sibling("jevify_plugin_admin", "jevify_admin.py")
    recipe = str(cfg.get("recipe") or "").strip()
    binary = admin.binary_path()
    managed = bool(cfg.get("manage_sidecar"))
    if managed and recipe and binary:
        port = urlparse(cfg.get("base_url") or "").port or 8600
        import shlex
        mgr.add(_NAME, shlex.join(
            [binary, "serve", recipe, "--port", str(port)]),
            restart=True, restart_delay=10, kill_signal=15)
    elif managed:
        log.warning("jev manage_sidecar needs a recipe and the jevify binary")

    async def _jevify_start() -> None:
        # No-op until the UI or manual config registers the sidecar.
        await mgr.start_one(_NAME)

    async def _jevify_stop() -> None:
        await mgr.remove(_NAME)

    # Always record cleanup for an enabled plugin: the UI can add the process
    # later, after runtime.plugins has snapshotted these hooks.
    state.startup_hooks.append(_jevify_start)
    state.shutdown_hooks.append(_jevify_stop)

    if managed and recipe and binary:
        # Hot-enable path: boot already happened, so start now. At boot the
        # startup hook covers it and there is no running loop yet.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            from runtime.proc import spawn_background
            spawn_background(mgr.start_one(_NAME), name="jevify-sidecar-start")

    if app is None:
        return

    @app.get("/api/admin/plugins/jev/api/overview")
    async def overview():
        from runtime.serving import health_now
        settings = {key: cfg.get(key) for key in (
            "preset", "base_url", "model", "recipe", "manage_sidecar",
            "route", "route_threshold", "backend")}
        base_url = str(cfg.get("base_url") or "").rstrip("/")
        health = None
        if base_url and not cfg.get("backend"):
            health = await health_now(base_url)
        return {
            "settings": settings,
            "binary": admin.binary_path(),
            "process": mgr.status().get(_NAME),
            "health": health,
            "presets": await admin.preset_options(runtime.config),
        }

    @app.post("/api/admin/plugins/jev/api/configure")
    async def configure(request: Request):
        if _CONFIGURE_LOCK.locked():
            raise HTTPException(409, "another jevify configuration is running")
        body = (await request.json()) or {}
        name = str(body.get("preset") or "").strip()
        if not name:
            raise HTTPException(400, "preset is required")
        async with _CONFIGURE_LOCK:
            try:
                preset, served, dialect = await admin.selected_preset(
                    runtime.config, name)
                recipe_path, probed = await admin.prepare_recipe(
                    preset, served, dialect, reprobe=bool(body.get("reprobe")))
                port = admin.choose_sidecar_port(
                    mgr, str(cfg.get("base_url") or ""))
                base_url = f"http://127.0.0.1:{port}"
                updates = {
                    "plugins.jev.preset": name,
                    "plugins.jev.backend": "",
                    "plugins.jev.base_url": base_url,
                    "plugins.jev.model": served,
                    "plugins.jev.recipe": str(recipe_path),
                    "plugins.jev.manage_sidecar": True,
                }
                users = state.users
                overrides = dict(users.get_config_overrides())
                overrides.update(updates)
                if not users.set_config_overrides(overrides):
                    raise admin.AdminError(500, "could not persist jevify settings")
                for dotpath, value in updates.items():
                    _set_nested(runtime.config, dotpath, value)
                    cfg[dotpath.rsplit(".", 1)[-1]] = value
                process = await admin.restart_sidecar(
                    mgr, recipe_path, base_url)
            except admin.AdminError as e:
                raise HTTPException(e.status, e.detail) from e
        return {"ok": True, "preset": name, "model": served,
                "recipe": str(recipe_path), "base_url": base_url,
                "probed": probed, "process": process}

    @app.post("/api/admin/plugins/jev/api/test")
    async def test_decision():
        if not cfg.get("base_url"):
            raise HTTPException(409, "configure a jevify preset first")
        client = _load_sibling("jev_plugin_client", "jev_client.py")
        questions = {"health": {
            "type": "noul",
            "instructions": "Is the service described as healthy?"}}
        try:
            answers = await asyncio.to_thread(
                client.decide, runtime.config,
                "The local decision service is healthy.", questions,
                client.settings(runtime.config)["timeout_s"])
        except client.JevError as e:
            raise HTTPException(502, str(e)) from e
        return {"ok": True, "answers": answers}
