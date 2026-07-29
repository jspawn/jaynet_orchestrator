"""Voice slots: STT/TTS presets wired into the live config + process manager.

The slotted `stt` preset (kind=stt) becomes a managed whisper-server process
(named WHISPER_PROC, next to brain/specialist in admin → Processes); the
slotted `tts` preset (kind=tts) only rewrites voice.tts.command — piper stays
an on-demand command, no process. Empty slot = feature off: voice.stt.url /
voice.tts.command fall back to the YAML defaults (pass the raw YAML config as
`yaml_config` so apply() can RESTORE them live after an unslot).

apply() is safe both at register time (before ProcessManager.start_all — the
whisper process is only added, and joins the boot start) and after admin
edits (manager already started — a re-added process is started immediately).
"""
from __future__ import annotations

import shlex

from runtime.preset_store import resolve_slot

WHISPER_PROC = "whisper"


def parse_conf(text: str) -> dict:
    """KEY=VALUE lines (same convention start-model.sh parses: blank lines and
    # comments ignored, inline comments stripped, whitespace trimmed)."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.split("#", 1)[0].strip()
        out[key.strip()] = val
    return out


def build_whisper_command(preset: dict) -> str:
    """whisper-server launch command for an stt preset; "" when the preset is
    unusable (no binary/port/MODEL_PATH). ARGS pass through verbatim (admin-
    only config)."""
    conf = parse_conf((preset or {}).get("conf") or "")
    model = conf.get("MODEL_PATH") or ""
    binary = str((preset or {}).get("binary") or "").strip()
    port = (preset or {}).get("port")
    if not model or not binary or not port:
        return ""
    parts = [shlex.quote(binary), "-m", shlex.quote(model),
             "--host", "127.0.0.1", "--port", str(int(port))]
    if str((preset or {}).get("gpu") or "").strip():
        parts += ["-ngl", "99"]
    if conf.get("ARGS"):
        parts.append(conf["ARGS"])
    return " ".join(parts)


async def apply(config: dict, proc_mgr, yaml_config: dict | None = None) -> None:
    """Sync the whisper process + voice.stt.url / voice.tts.command with the
    slotted stt/tts presets. yaml_config = the raw runtime.yaml dict, used to
    restore the fallback url/command when a slot is empty."""
    stt = resolve_slot(config, "stt")
    tts = resolve_slot(config, "tts")
    if (stt.get("kind") or "llama") != "stt":   # kind guard: wrong preset → off
        stt = {}
    if (tts.get("kind") or "llama") != "tts":
        tts = {}
    voice = config.setdefault("voice", {})
    yvoice = (yaml_config or {}).get("voice") or {}

    # ---- STT: managed whisper-server process + url ----
    cmd = build_whisper_command(stt)
    cur = proc_mgr.status().get(WHISPER_PROC)
    if cmd:
        gpu = str(stt.get("gpu") or "").strip()
        env = {"HIP_VISIBLE_DEVICES": gpu} if gpu else {}
        if cur is not None and cur.get("command") != cmd:
            await proc_mgr.remove(WHISPER_PROC)     # command changed: re-add
            cur = None
        if cur is None:
            proc_mgr.add(WHISPER_PROC, cmd, env=env,
                         restart=True, restart_delay=10, kill_signal=15)
            if proc_mgr.started:                    # live edit, not boot
                await proc_mgr.start_one(WHISPER_PROC)
        port = stt.get("port")
        if port:
            voice.setdefault("stt", {})["url"] = (
                f"http://127.0.0.1:{int(port)}/inference")
    else:
        if cur is not None:
            await proc_mgr.remove(WHISPER_PROC)     # unslotted: feature off
        yurl = (yvoice.get("stt") or {}).get("url")  # restore YAML fallback
        if yurl:
            voice.setdefault("stt", {})["url"] = yurl

    # ---- TTS: command only (piper stays on-demand) ----
    tts_cmd = parse_conf(tts.get("conf") or "").get("COMMAND") or ""
    if tts_cmd:
        voice.setdefault("tts", {})["command"] = tts_cmd
    else:
        ycmd = (yvoice.get("tts") or {}).get("command")  # restore YAML fallback
        if ycmd:
            voice.setdefault("tts", {})["command"] = ycmd
