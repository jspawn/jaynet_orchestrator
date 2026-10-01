"""omnivoice tts-server lifecycle + speak/clone calls.

One module-level TtsServer per JayNet process (loaded once via
spec_from_file_location from tools/voice.py). All calls are async — the
tool's execute() runs on the app event loop, so the keep-warm reaper is a
plain asyncio task.

Unlike imagegen there is NO slot hibernation: the Q8_0 pair (Qwen3 0.6B LM
+ codec) needs ~1 GB, which fits next to brain + specialist. The server is
still started lazily and reaped after `keep_warm_s` idle so an unused TTS
backend never holds VRAM.
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
from typing import Any

log = logging.getLogger("omnivoice")

_MODEL_DIR = "/srv/models/Serveurperso/OmniVoice-GGUF"

_DEFAULTS = {
    "binary": "/srv/jaynet-bin/omnivoice.cpp.rocm/bin/tts-server",
    "model": "",                 # empty = auto: $_MODEL_DIR/omnivoice-base-*.gguf
    "codec": "",                 # empty = auto: $_MODEL_DIR/omnivoice-tokenizer-*.gguf
    "port": 8730,
    "backend": "",               # GGML_BACKEND override ("" = runtime picks)
    "language": "English",
    "voice": "",                 # default registered voice for speak
    "keep_warm_s": 600,
    "health_timeout_s": 300,
    "gen_timeout_s": 300,
    "extra_args": "",
}


def settings(config: dict) -> dict:
    cfg = ((config or {}).get("plugins") or {}).get("omnivoice") or {}
    out: dict[str, Any] = dict(_DEFAULTS)
    for k in out:
        if k in cfg and cfg[k] is not None:
            out[k] = cfg[k]
    out["port"] = int(out["port"])
    out["keep_warm_s"] = float(out["keep_warm_s"])
    out["health_timeout_s"] = float(out["health_timeout_s"])
    out["gen_timeout_s"] = float(out["gen_timeout_s"])
    # Auto-discovery: an unset model/codec falls back to the standard model
    # dir and picks the first matching GGUF, so any quant drop-in works
    # without a config edit.
    for key, pat in (("model", "omnivoice-base-*.gguf"),
                     ("codec", "omnivoice-tokenizer-*.gguf")):
        if not str(out[key] or "").strip():
            hits = sorted(Path(_MODEL_DIR).glob(pat))
            if hits:
                out[key] = str(hits[0])
    return out


class OmnivoiceError(Exception):
    """Missing binary/model, tts-server won't come up, or a call failed."""


class TtsServer:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self._reaper: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # -- config validation ----------------------------------------------------
    @staticmethod
    def _check_files(cfg: dict) -> None:
        missing = []
        for key, label in (("binary", "tts-server binary"),
                           ("model", "base LM GGUF"),
                           ("codec", "codec/tokenizer GGUF")):
            p = str(cfg.get(key) or "")
            if not p or not Path(p).is_file():
                missing.append(f"{label} ({key}={p!r})")
        if missing:
            raise OmnivoiceError(
                "omnivoice not installed: missing " + "; ".join(missing) +
                " — see plugins/omnivoice/README.md")

    # -- HTTP -----------------------------------------------------------------
    def _url(self, cfg: dict, path: str) -> str:
        return f"http://127.0.0.1:{cfg['port']}{path}"

    def _healthy(self, cfg: dict) -> bool:
        try:
            with urllib.request.urlopen(self._url(cfg, "/health"),
                                        timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def _post(self, cfg: dict, path: str, body: dict,
              timeout: float) -> tuple[bytes | None, dict | None, str | None]:
        """POST JSON; returns (raw_bytes, parsed_json, error). Raw bytes for
        the WAV speech response, parsed JSON for the voices endpoints."""
        req = urllib.request.Request(
            self._url(cfg, path), data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                ctype = r.headers.get("Content-Type") or ""
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            return None, None, f"HTTP {e.code}: {detail}"
        except Exception as e:
            return None, None, str(e)
        if "json" in ctype:
            try:
                return None, json.loads(raw), None
            except Exception:
                return None, None, "server returned invalid JSON"
        return raw, None, None

    # -- tts-server lifecycle ---------------------------------------------------
    async def ensure_up(self, cfg: dict) -> None:
        if self.proc is not None and self.proc.poll() is None \
                and await asyncio.to_thread(self._healthy, cfg):
            return
        await self.down()
        self._check_files(cfg)
        lib = str(Path(cfg["binary"]).resolve().parent.parent / "lib")
        env = dict(os.environ)
        if Path(lib).is_dir():
            env["LD_LIBRARY_PATH"] = lib + ":" + env.get("LD_LIBRARY_PATH", "")
        backend = str(cfg.get("backend") or "").strip()
        if backend:
            env["GGML_BACKEND"] = backend
        cmd = [cfg["binary"],
               "--model", cfg["model"],
               "--codec", cfg["codec"],
               "--host", "127.0.0.1", "--port", str(cfg["port"]),
               "--lang", str(cfg.get("language") or "None")]
        cmd += str(cfg.get("extra_args") or "").split()
        log.info("starting tts-server on :%s", cfg["port"])
        proc = await asyncio.to_thread(
            subprocess.Popen, cmd, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.proc = proc
        deadline = time.monotonic() + cfg["health_timeout_s"]
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.proc = None
                raise OmnivoiceError(
                    "tts-server exited during startup — check binary/models "
                    "(plugins/omnivoice/README.md)")
            if await asyncio.to_thread(self._healthy, cfg):
                return
            await asyncio.sleep(3)
        await self.down()
        raise OmnivoiceError(
            f"tts-server not healthy after {cfg['health_timeout_s']:.0f}s")

    async def down(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, lambda: proc.wait(timeout=15))
                except Exception:
                    proc.kill()

    # -- speech / voices ----------------------------------------------------------
    async def speak(self, config: dict, args: dict) -> dict:
        """Text → WAV in DATA/audio. Returns the artifact path + metadata."""
        cfg = settings(config)
        async with self._lock:
            await self.ensure_up(cfg)
            body = {
                "input": args["text"],
                "response_format": "wav",
                "language": str(args.get("language") or cfg["language"]),
            }
            voice = str(args.get("voice") or cfg["voice"] or "").strip()
            if voice:
                body["voice"] = voice
            instructions = str(args.get("instructions") or "").strip()
            if instructions:
                body["instructions"] = instructions
            if args.get("seed") is not None:
                body["seed"] = int(args["seed"])
            raw, _, err = await asyncio.to_thread(
                self._post, cfg, "/v1/audio/speech", body,
                cfg["gen_timeout_s"])
            if err is not None:
                raise OmnivoiceError(f"speech call failed: {err}")
            if not raw or len(raw) < 44 or raw[:4] != b"RIFF":
                raise OmnivoiceError(
                    "tts-server returned no WAV: " + (raw or b"")[:200].decode(
                        "utf-8", "replace"))
            from runtime import paths
            dest = paths.DATA / "audio"
            dest.mkdir(parents=True, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}.wav"
            (dest / name).write_bytes(raw)
            self._rearm_reaper(config, args)
            return {"path": str(dest / name), "bytes": len(raw),
                    "voice": voice or "(voice design)",
                    "language": body["language"],
                    "keep_warm_s": cfg["keep_warm_s"]}

    async def clone(self, config: dict, name: str, ref_text: str,
                    wav_bytes: bytes) -> dict:
        """Register a cloned voice server-side (WAV encoded by the server)."""
        cfg = settings(config)
        async with self._lock:
            await self.ensure_up(cfg)
            body = {"name": name, "ref_text": ref_text,
                    "wav_b64": base64.b64encode(wav_bytes).decode()}
            _, out, err = await asyncio.to_thread(
                self._post, cfg, "/v1/audio/voices", body,
                cfg["gen_timeout_s"])
            if err is not None:
                raise OmnivoiceError(f"voice registration failed: {err}")
            self._rearm_reaper(config, {})
            return {"name": name, "server_response": out}

    async def voices(self, config: dict) -> list[str]:
        cfg = settings(config)
        async with self._lock:
            await self.ensure_up(cfg)
            _, out, err = await asyncio.to_thread(
                self._get, cfg, "/v1/audio/voices")
            if err is not None:
                raise OmnivoiceError(f"voice list failed: {err}")
            self._rearm_reaper(config, {})
            return sorted((out or {}).get("voices") or [])

    def _get(self, cfg: dict, path: str) -> tuple[None, dict | None, str | None]:
        try:
            with urllib.request.urlopen(self._url(cfg, path), timeout=10) as r:
                return None, json.loads(r.read()), None
        except Exception as e:
            return None, None, str(e)

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
                                                 "omnivoice-keep-warm")
            return
        self._reaper = proc.spawn_background(self._reap_after(delay),
                                             "omnivoice-keep-warm")

    async def _reap_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._reap()
        except asyncio.CancelledError:
            pass

    async def _reap(self) -> None:
        async with self._lock:
            log.info("keep-warm expired — tts-server down")
            await self.down()


SERVER = TtsServer()
