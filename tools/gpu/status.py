"""GPU telemetry — so the agent knows if there's room before launching a job.

Wraps rocm-smi (preferred, JSON) with an amd-smi fallback, parsed defensively
because field names drift between ROCm releases. Per GPU it reports VRAM
used/total, utilisation, temperature and power where available, and always
includes the raw tool output so nothing is lost if a field name changed.

NOTE: parsing is best-effort against ROCm 7.x rocm-smi. If a field comes back
null on your box, run `rocm-smi --showmeminfo vram --showuse --showtemp
--showpower --json` and adjust the substring keys in _parse_rocm_smi below.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult

# Where ROCm SMI tools live when not on PATH. systemd services get a minimal
# PATH that usually omits these, so look here before giving up.
_ROCM_DIRS = ["/opt/rocm/bin"]


def _resolve(name: str, ctx: ToolContext) -> str | None:
    """Find a GPU tool: explicit config path -> PATH -> standard ROCm dirs."""
    cfg = (ctx.config.get("tools", {}).get("gpu", {}) or {})
    override = cfg.get(name.replace("-", "_") + "_path")  # e.g. rocm_smi_path
    if override and Path(override).exists():
        return override
    found = shutil.which(name)
    if found:
        return found
    dirs = list(_ROCM_DIRS) + sorted(glob.glob("/opt/rocm-*/bin")) + \
        list(cfg.get("bin_dirs") or [])
    for d in dirs:
        cand = Path(d) / name
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None


async def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"{cmd[0]} timed out"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _find(d: dict, *needles: str):
    """Return the first value whose key contains any needle (case-insensitive)."""
    low = {k.lower(): v for k, v in d.items()}
    for n in needles:
        for k, v in low.items():
            if n.lower() in k:
                return v
    return None


def _to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).strip().rstrip("%").strip())
    except (ValueError, TypeError):
        return None


def _parse_rocm_smi(raw: str) -> list[dict]:
    """rocm-smi --json => {'card0': {...}, 'card1': {...}} (key names vary)."""
    data = json.loads(raw)
    gpus = []
    for card, fields in data.items():
        cl = card.lower()
        if not (cl.startswith("card") or cl.startswith("gpu")):
            continue
        if not isinstance(fields, dict):
            continue
        total = _to_float(_find(fields, "VRAM Total Memory", "vram total"))
        used = _to_float(_find(fields, "VRAM Total Used Memory", "vram total used",
                               "used memory"))
        # rocm-smi reports VRAM in bytes; convert to GiB if it looks like bytes.
        def gib(x):
            return round(x / (1024 ** 3), 2) if (x and x > 1 << 20) else x
        gpus.append({
            "card": card,
            "name": _find(fields, "Card series", "Card model", "Device Name", "product name"),
            "vram_used_gib": gib(used),
            "vram_total_gib": gib(total),
            "vram_used_pct": (round(used / total * 100, 1) if (used and total) else None),
            "gpu_util_pct": _to_float(_find(fields, "GPU use (%)", "gpu use", "gfx activity")),
            "temp_c": _to_float(_find(fields, "Temperature (Sensor edge)",
                                      "Temperature (Sensor junction)", "edge", "temperature")),
            "power_w": _to_float(_find(fields, "Average Graphics Package Power",
                                       "current socket power", "power")),
        })
    return gpus


class GpuStatus(Tool):
    name = "gpu.status"
    description = ("Report per-GPU VRAM used/total, utilisation, temperature and "
                  "power for the AMD GPUs (ROCm). Call before launching a job to "
                  "check there's headroom.")
    parameters = {
        "type": "object",
        "properties": {
            "raw": {"type": "boolean", "default": False,
                    "description": "Include the raw tool output alongside parsed fields."},
        },
        "required": [],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        include_raw = bool(args.get("raw"))

        # Preferred: rocm-smi JSON
        rocm_smi = _resolve("rocm-smi", ctx)
        if rocm_smi:
            rc, out, err = await _run([
                rocm_smi, "--showmeminfo", "vram", "--showuse",
                "--showtemp", "--showpower", "--json",
            ])
            if rc == 0 and out.strip():
                try:
                    gpus = _parse_rocm_smi(out)
                    result = {"backend": "rocm-smi", "gpus": gpus, "count": len(gpus)}
                    if include_raw or not gpus:
                        result["raw"] = out[:8000]
                    return ToolResult(status="ok", result=result)
                except Exception as e:
                    # Fall through to raw passthrough rather than failing hard.
                    return ToolResult(status="ok", result={
                        "backend": "rocm-smi",
                        "parse_error": f"{type(e).__name__}: {e}",
                        "raw": out[:8000],
                    })
            # rocm-smi present but errored — surface it before trying amd-smi
            rocm_err = err.strip() or f"rocm-smi rc {rc}"
        else:
            rocm_err = "rocm-smi not found"

        # Fallback: amd-smi (newer ROCm). Schema varies a lot; passthrough raw.
        amd_smi = _resolve("amd-smi", ctx)
        if amd_smi:
            rc, out, err = await _run([amd_smi, "monitor", "-puttmv"])
            if rc == 0 and out.strip():
                return ToolResult(status="ok", result={
                    "backend": "amd-smi",
                    "note": "amd-smi parsed output not structured; see raw",
                    "raw": out[:8000],
                })
            return ToolResult(status="error", result=None,
                              error=f"amd-smi failed: {err.strip() or rc}; "
                                    f"rocm-smi: {rocm_err}")

        return ToolResult(
            status="error", result=None,
            error=(f"no GPU tool available ({rocm_err}; amd-smi not found). "
                   "Checked PATH and /opt/rocm/bin. Fix: add /opt/rocm/bin to the "
                   "service PATH (and SupplementaryGroups=render video), or set "
                   "tools.gpu.rocm_smi_path / amd_smi_path in runtime.yaml."))
