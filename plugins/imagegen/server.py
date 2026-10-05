"""imagegen sd-server lifecycle + slot swap management.

One module-level SdServer per JayNet process (loaded once via
spec_from_file_location from tools/image.py). All calls are async — the
tool's execute() runs on the app event loop, so the keep-warm reaper is a
plain asyncio task.

Swap semantics: a generation hibernates the configured slots (`swap_slots`
list + the back-compat single `swap_slot`; default ["specialist"] — the big
VRAM tenants; a big generation may need BOTH the specialist's and the
brain's VRAM) via the in-process ProcessManager, brings sd-server up, and
only after `keep_warm_s` without further generations kills sd-server and
restarts the slots (reverse-of-stop order — LIFO, like a dependency stack).
A hibernated BRAIN is restored immediately after the generation POST
instead: the parent run's very next turn calls it, so the plugin restarts
it and polls its port until it answers (`restore_ready_timeout_s`) before
returning the tool result. In-flight work on the slots is NOT waited out —
hibernating the brain while ANOTHER run is mid-turn on it breaks that run.
Single-user box; image requests are user-initiated and rare — the same
documented trade as the delegation hibernation in
tools/specialist/delegate.py.
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
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("imagegen")

_DEFAULTS = {
    "sd_binary": "/srv/jaynet-bin/sd-server",
    "diffusion_model": "",
    "text_encoder": "",
    "vae": "",
    "port": 8720,
    "gpu": "0",
    "swap_slot": "specialist",
    "swap_slots": [],
    "keep_warm_s": 600,
    "steps": 20,
    "cfg_scale": 2.5,
    "size": "1024x1024",
    "health_timeout_s": 300,
    "gen_timeout_s": 900,
    "restore_ready_timeout_s": 300,
    "extra_args": "",
}


def settings(config: dict) -> dict:
    cfg = ((config or {}).get("plugins") or {}).get("imagegen") or {}
    out: dict[str, Any] = dict(_DEFAULTS)
    for k in out:
        if k in cfg and cfg[k] is not None:
            out[k] = cfg[k]
    out["port"] = int(out["port"])
    out["keep_warm_s"] = float(out["keep_warm_s"])
    out["steps"] = int(out["steps"])
    out["health_timeout_s"] = float(out["health_timeout_s"])
    out["gen_timeout_s"] = float(out["gen_timeout_s"])
    out["restore_ready_timeout_s"] = float(out["restore_ready_timeout_s"])
    # swap_slots (list) + swap_slot (single string, back-compat) merge into
    # one ordered unique list: swap_slots entries first, then swap_slot when
    # it isn't already there. Neither set → the default ["specialist"].
    # Set swap_slot: "" to hibernate ONLY the swap_slots entries.
    slots = []
    raw = out.get("swap_slots")
    if isinstance(raw, str):
        raw = [raw]
    for name in list(raw or []) + [out.get("swap_slot")]:
        name = str(name or "").strip()
        if name and name not in slots:
            slots.append(name)
    out["swap_slots"] = slots
    return out


class ImagegenError(Exception):
    """Missing binary/model, sd-server won't come up, or generation failed."""


class SdServer:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.slots_stopped: list[str] = []
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
    async def _stop_slots(self, cfg: dict) -> None:
        """Hibernate every configured slot that is alive, in list order;
        only the ones actually stopped are remembered for the restore."""
        slots = list(cfg.get("swap_slots") or [])
        if not slots:
            return
        from runtime import process_manager
        pm = process_manager.CURRENT
        if pm is None:
            log.warning("no process manager — slots %s stay up (VRAM risk)",
                        slots)
            return
        st = pm.status()
        for slot in slots:
            if (st.get(slot) or {}).get("alive"):
                log.info("hibernating slot %s for image generation", slot)
                await pm.stop_one(slot)
                self.slots_stopped.append(slot)

    @staticmethod
    def _slot_port(config: dict, slot: str, pm) -> int | None:
        """The HTTP port a restored slot should answer on: the slot preset's
        port (same mapping the Models admin uses — process names mirror slot
        names), else parsed from the managed process's launch command
        (--port N). None = undiscoverable → no ready wait for that slot."""
        try:
            from runtime.preset_store import resolve_slot
            port = resolve_slot(config, slot).get("port")
            if port:
                return int(port)
        except Exception:
            pass
        import re
        cmd = str((pm.status().get(slot) or {}).get("command") or "")
        m = re.search(r"--port[ =](\d+)", cmd)
        return int(m.group(1)) if m else None

    async def _wait_ready(self, slot: str, port: int, timeout_s: float) -> bool:
        """Poll the restored slot's /v1/models until it answers — the parent
        run's next turn may call it immediately (the brain especially, when
        it was hibernated for the VRAM)."""
        url = f"http://127.0.0.1:{port}/v1/models"
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                with await asyncio.to_thread(
                        urllib.request.urlopen, url, timeout=3) as r:
                    if r.status == 200:
                        log.info("slot %s answering on :%s again", slot, port)
                        return True
            except Exception:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(3.0, remaining))
        log.warning("slot %s not answering on :%s after %.0fs — the next "
                    "model calls against it will fail until it finishes "
                    "loading%s", slot, port, timeout_s,
                    " (the BRAIN — this run's next turn included)"
                    if slot == "brain" else "")
        return False

    async def _restore_slot(self, config: dict, slot: str) -> bool:
        """start_one + bounded ready wait when the port is discoverable.
        Returns True when the slot did not answer in time."""
        from runtime import process_manager
        pm = process_manager.CURRENT
        if pm is None:
            return False
        log.info("restoring slot %s after image generation", slot)
        await pm.start_one(slot)
        port = self._slot_port(config, slot, pm)
        if not port:
            return False
        timeout = settings(config)["restore_ready_timeout_s"]
        return not await self._wait_ready(slot, port, timeout)

    async def _wake_slots(self, config: dict,
                          only: set[str] | None = None) -> list[str]:
        """Restart hibernated slots in REVERSE stop order — LIFO, like a
        dependency stack (the last tenant hibernated is the first brought
        back; with the typical ["specialist", "brain"] list the brain comes
        up first, so the parent run's next turn isn't kept waiting).
        `only` restricts the restore to a subset (the brain is woken right
        after the generation POST; the other slots wait out the keep-warm
        window). Cancel-safe: a CancelledError mid-list is recorded, the
        remaining slots are still started, and the error is re-raised at
        the end — cleanup always covers every stopped slot. Returns the
        slots that failed the ready wait."""
        slots = [s for s in self.slots_stopped if only is None or s in only]
        if not slots:
            return []
        self.slots_stopped = [s for s in self.slots_stopped if s not in slots]
        not_ready: list[str] = []
        cancelled: asyncio.CancelledError | None = None
        for slot in reversed(slots):
            try:
                if await self._restore_slot(config, slot):
                    not_ready.append(slot)
            except asyncio.CancelledError as e:
                cancelled = cancelled or e
                log.warning("cancelled while restoring slot %s — restoring "
                            "the remaining slots anyway", slot)
            except Exception:
                log.exception("slot %s failed to restart", slot)
        if cancelled is not None:
            raise cancelled
        return not_ready

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
            await self._stop_slots(cfg)
            hibernated = list(self.slots_stopped)
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
                name = (time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
                        f"-{uuid.uuid4().hex[:8]}.png")
                (dest / name).write_bytes(png)
                not_ready: list[str] = []
                if "brain" in self.slots_stopped:
                    # The parent's very next turn calls the brain — restore
                    # it NOW, not after the keep-warm window, and don't
                    # return until it answers (or the bounded wait gives up).
                    not_ready = await self._wake_slots(config, only={"brain"})
                self._rearm_reaper(config, args)
                return {"path": str(dest / name), "bytes": len(png),
                        "size": body["size"], "steps": body["steps"],
                        "slots_hibernated": hibernated,
                        # back-compat single-slot key (v1 readers)
                        "slot_hibernated": hibernated[0] if hibernated else "",
                        "restore_not_ready": not_ready,
                        "keep_warm_s": cfg["keep_warm_s"]}
            except BaseException:
                # CancelledError too (BaseException, not caught by `except
                # Exception`): never leak the hibernated slots, and re-arm
                # the reaper so the sd-server a cancel stranded mid-POST
                # still comes down after the keep-warm window.
                self._rearm_reaper(config, args)
                await self._wake_slots(config)
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
            self._reaper = proc.spawn_background(self._reap(config),
                                                 "imagegen-keep-warm")
            return
        self._reaper = proc.spawn_background(self._reap_after(delay, config),
                                             "imagegen-keep-warm")

    async def _reap_after(self, delay: float, config: dict) -> None:
        try:
            await asyncio.sleep(delay)
            await self._reap(config)
        except asyncio.CancelledError:
            pass

    async def _reap(self, config: dict) -> None:
        async with self._lock:
            log.info("keep-warm expired — sd-server down, slots back")
            await self.down()
            await self._wake_slots(config)


SERVER = SdServer()
