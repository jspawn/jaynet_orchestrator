"""imagegen sd-server lifecycle + slot swap management.

One module-level SdServer per JayNet process (loaded once via
spec_from_file_location from tools/image.py). All calls are async — the
tool's execute() runs on the app event loop, so the keep-warm reaper is a
plain asyncio task.

Swap semantics: a generation hibernates `swap_slot` (default specialist —
the big VRAM tenant) via the in-process ProcessManager, brings sd-server
up, and only after `keep_warm_s` without further generations kills
sd-server and restarts the slot. In-flight delegations on the slot are
NOT waited out — image requests are user-initiated and rare; the trade
is documented in the README.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("imagegen")

_DEFAULTS = {
    "sd_binary": "/srv/jaynet-bin/sd-server",
    "diffusion_model": "",
    "text_encoder": "",
    "vae": "",
    "port": 8720,
    "gpu": "0",
    "swap_slot": "specialist",
    "keep_warm_s": 600,
    "steps": 20,
    "cfg_scale": 2.5,
    "size": "1024x1024",
    "health_timeout_s": 300,
    "gen_timeout_s": 900,
    "extra_args": "",
}


def settings(config: dict) -> dict:
    cfg = ((config or {}).get("plugins") or {}).get("imagegen") or {}
    out = dict(_DEFAULTS)
    for k in out:
        if k in cfg and cfg[k] is not None:
            out[k] = cfg[k]
    out["port"] = int(out["port"])
    out["keep_warm_s"] = float(out["keep_warm_s"])
    out["steps"] = int(out["steps"])
    out["health_timeout_s"] = float(out["health_timeout_s"])
    out["gen_timeout_s"] = float(out["gen_timeout_s"])
    return out


class ImagegenError(Exception):
    """Missing binary/model, sd-server won't come up, or generation failed."""


class SdServer:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.slot_stopped: str | None = None
        self._reaper: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # -- config validation ----------------------------------------------------
    @staticmethod
    def _check_files(cfg: dict) -> None:
        missing = []
        for key, label in (("sd_binary", "sd-server binary"),
                           ("diffusion_model", "diffusion model (DiT GGUF)"),
                           ("text_encoder", "text encoder"),
                           ("vae", "VAE")):
            p = str(cfg.get(key) or "")
            if not p or not Path(p).is_file():
                missing.append(f"{label} ({key}={p!r})")
        if missing:
            raise ImagegenError(
                "imagegen not installed: missing " + "; ".join(missing) +
                " — see plugins/imagegen/README.md")

    # -- HTTP -----------------------------------------------------------------
    def _url(self, cfg: dict, path: str) -> str:
        return f"http://127.0.0.1:{cfg['port']}{path}"

    def _healthy(self, cfg: dict) -> bool:
        try:
            with urllib.request.urlopen(self._url(cfg, "/v1/models"),
                                        timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    # -- slot swap --------------------------------------------------------------
    async def _stop_slot(self, cfg: dict) -> None:
        slot = str(cfg.get("swap_slot") or "").strip()
        if not slot:
            return
        from runtime import process_manager
        pm = process_manager.CURRENT
        if pm is None:
            log.warning("no process manager — slot %s stays up (VRAM risk)",
                        slot)
            return
        st = (pm.status().get(slot) or {})
        if st.get("alive"):
            log.info("hibernating slot %s for image generation", slot)
            await pm.stop_one(slot)
        self.slot_stopped = slot

    async def _wake_slot(self) -> None:
        if not self.slot_stopped:
            return
        slot, self.slot_stopped = self.slot_stopped, None
        from runtime import process_manager
        pm = process_manager.CURRENT
        if pm is not None:
            log.info("restoring slot %s after image generation", slot)
            await pm.start_one(slot)

    # -- sd-server lifecycle ----------------------------------------------------
    async def ensure_up(self, cfg: dict) -> None:
        if self.proc is not None and self.proc.poll() is None \
                and await asyncio.to_thread(self._healthy, cfg):
            return
        await self.down()
        self._check_files(cfg)
        lib = str(Path(cfg["sd_binary"]).resolve().parent.parent / "lib")
        env = dict(os.environ)
        if Path(lib).is_dir():
            env["LD_LIBRARY_PATH"] = lib + ":" + env.get("LD_LIBRARY_PATH", "")
        gpu = str(cfg.get("gpu") or "").strip()
        if gpu:
            env["HIP_VISIBLE_DEVICES"] = gpu
        cmd = [cfg["sd_binary"],
               "--diffusion-model", cfg["diffusion_model"],
               "--llm", cfg["text_encoder"],
               "--vae", cfg["vae"],
               "-l", "127.0.0.1", "--listen-port", str(cfg["port"]),
               "--offload-to-cpu"]
        extra = str(cfg.get("extra_args") or "").split()
        cmd += extra
        log.info("starting sd-server on :%s", cfg["port"])
        self.proc = await asyncio.to_thread(
            subprocess.Popen, cmd, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + cfg["health_timeout_s"]
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.proc = None
                raise ImagegenError(
                    "sd-server exited during startup — check binary/models "
                    "(plugins/imagegen/README.md)")
            if await asyncio.to_thread(self._healthy, cfg):
                return
            await asyncio.sleep(3)
        await self.down()
        raise ImagegenError(
            f"sd-server not healthy after {cfg['health_timeout_s']:.0f}s")

    async def down(self) -> None:
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, lambda: self.proc.wait(timeout=15))
                except Exception:
                    self.proc.kill()
            self.proc = None

    # -- generation ---------------------------------------------------------------
    async def generate(self, config: dict, args: dict) -> dict:
        cfg = settings(config)
        async with self._lock:
            await self._stop_slot(cfg)
            try:
                await self.ensure_up(cfg)
                body = {
                    "prompt": args["prompt"],
                    "negative_prompt": args.get("negative_prompt") or "",
                    "size": args.get("size") or cfg["size"],
                    "n": 1,
                    "steps": int(args.get("steps") or cfg["steps"]),
                    "cfg_scale": float(args.get("cfg_scale") or cfg["cfg_scale"]),
                    "output_format": "png",
                }
                if args.get("seed") is not None:
                    body["seed"] = int(args["seed"])
                req = urllib.request.Request(
                    self._url(cfg, "/v1/images/generations"),
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"})
                try:
                    with await asyncio.to_thread(
                            urllib.request.urlopen, req,
                            timeout=cfg["gen_timeout_s"]) as r:
                        out = json.loads(r.read())
                except Exception as e:
                    raise ImagegenError(f"generation call failed: {e}") from e
                data = (out.get("data") or [])
                if not data or not data[0].get("b64_json"):
                    raise ImagegenError(
                        "sd-server returned no image: "
                        + json.dumps(out)[:300])
                png = base64.b64decode(data[0]["b64_json"])
                from runtime import paths
                dest = paths.DATA / "images"
                dest.mkdir(parents=True, exist_ok=True)
                name = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}.png"
                (dest / name).write_bytes(png)
                self._rearm_reaper(config, args)
                return {"path": str(dest / name), "bytes": len(png),
                        "size": body["size"], "steps": body["steps"],
                        "slot_hibernated": str(cfg.get("swap_slot") or ""),
                        "keep_warm_s": cfg["keep_warm_s"]}
            except Exception:
                await self._wake_slot()
                raise

    # -- keep-warm reaper ---------------------------------------------------------
    def _rearm_reaper(self, config: dict, args: dict) -> None:
        if self._reaper is not None and not self._reaper.done():
            self._reaper.cancel()
        keep = args.get("keep_warm_s")
        cfg = settings(config)
        delay = float(keep) if keep is not None else cfg["keep_warm_s"]
        from runtime import proc  # tracked: named, logged, cancelled at shutdown
        if delay <= 0:
            self._reaper = proc.spawn_background(self._reap(),
                                                 "imagegen-keep-warm")
            return
        self._reaper = proc.spawn_background(self._reap_after(delay),
                                             "imagegen-keep-warm")

    async def _reap_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._reap()
        except asyncio.CancelledError:
            pass

    async def _reap(self) -> None:
        async with self._lock:
            log.info("keep-warm expired — sd-server down, slot back")
            await self.down()
            await self._wake_slot()


SERVER = SdServer()
