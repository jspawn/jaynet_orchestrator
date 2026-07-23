"""Apply the configured serving posture at orchestrator startup.

Goal: take the GPU-1 model OFF systemd and have the orchestrator load it at boot
*through the serve layer* (via model.use), so it's serve-managed — and model.use
can then swap the GPU-1 slot (e.g. coder <-> another specialist) WITHOUT a
`systemctl stop`.

Wiring (three steps):
  1. Disable the systemd unit for the GPU-1 model:  systemctl --user disable --now llama-coder
  2. In runtime.yaml, set which presets to serve at boot:  models.boot: [coder]
  3. From the web app's startup event, fire this once (non-blocking):
         from runtime.boot_posture import apply_boot_posture
         asyncio.create_task(apply_boot_posture(runtime))

It's idempotent: model.use health-probes the preset's port first, so a web-app
restart won't relaunch an already-running (detached) server. Failures are logged,
never fatal — a bad boot preset must not stop the console from coming up.
"""
from __future__ import annotations

import asyncio
import logging

from runtime.tool_base import ToolContext
from tools.model.catalog import ModelUse

log = logging.getLogger("orch.boot")


async def apply_boot_posture(runtime, initial_delay: float = 3.0) -> list[dict]:
    """Serve each preset in `models.boot` via model.use. Returns a small report per
    preset. Runs as a background task; never raises."""
    cfg = getattr(runtime, "config", None) or {}
    boot = ((cfg.get("models") or {}).get("boot")) or []
    report: list[dict] = []
    if not boot:
        return report
    if initial_delay:
        await asyncio.sleep(initial_delay)          # let systemd-started servers settle
    ctx = ToolContext(request_id="boot", config=cfg, budget=None)
    tool = ModelUse()
    for preset in boot:
        try:
            res = await tool.execute({"preset": preset}, ctx)
            if res.status == "ok":
                status = (res.result or {}).get("status", "ok")
                log.info("boot posture: model.use(%s) -> %s", preset, status)
                report.append({"preset": preset, "ok": True, "status": status})
            else:
                log.warning("boot posture: model.use(%s) error: %s", preset, res.error)
                report.append({"preset": preset, "ok": False, "error": res.error})
        except Exception as e:                       # never let a boot preset crash startup
            log.warning("boot posture: model.use(%s) raised: %s: %s",
                        preset, type(e).__name__, e)
            report.append({"preset": preset, "ok": False, "error": f"{type(e).__name__}: {e}"})
    return report
