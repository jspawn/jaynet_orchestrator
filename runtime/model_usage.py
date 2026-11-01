"""Live RAM, VRAM and model-artifact disk usage for one running preset.

All probes are read-only and best-effort. Missing telemetry stays ``None`` —
a vendor tool that cannot attribute memory to a process must not turn into a
misleading zero. The admin route calls :func:`collect_usage` in a thread.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from runtime.serve_preset import parse_preset
from runtime.tool_base import ToolContext

_GIB = 1024**3


def process_tree(root_pid: int, proc_root: str | Path = "/proc") -> list[int]:
    """Root plus all descendants (vLLM/radiance workers hold much of the RAM)."""
    try:
        root = int(root_pid)
    except (TypeError, ValueError):
        return []
    children: dict[int, list[int]] = {}
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return [root]
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_bytes()
            ppid = int(stat.rsplit(b")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry.name))
    out: list[int] = []
    seen: set[int] = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(children.get(pid, ()))
    return out


def _pss_kb(text: str) -> int | None:
    for line in text.splitlines():
        if line.startswith("Pss:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def ram_usage(pids: list[int], proc_root: str | Path = "/proc") -> dict:
    """Summed proportional set size: includes mmap'd weights without counting
    shared pages once per worker. This is not the same as RSS or free-RAM delta."""
    total = 0
    readable = 0
    for pid in pids:
        try:
            kb = _pss_kb((Path(proc_root) / str(pid) / "smaps_rollup").read_text())
        except OSError:
            continue
        if kb is not None:
            total += kb
            readable += 1
    return {"used_gib": round(total / 1024**2, 2) if readable else None,
            "source": "process-tree PSS (smaps_rollup)" if readable else None,
            "processes": len(pids), "readable_processes": readable}


def _rocm_memory(raw: str) -> dict[int, int]:
    """PID → total VRAM bytes from ``rocm-smi --showpids``."""
    out = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        try:
            out[int(parts[0])] = int(parts[3])
        except ValueError:
            continue
    return out


def parse_rocm_pid_gpus(raw: str) -> dict[int, list[str]]:
    """PID → DRM card indices from ``rocm-smi --showpidgpus``.

    The GPU(s) column in --showpids is a COUNT, not an index (on a two-card
    box both single-card processes report 1), so it must never be used for
    placement.
    """
    out: dict[int, list[str]] = {}
    current: int | None = None
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "PID" and parts[1].isdigit():
            current = int(parts[1])
            out.setdefault(current, [])
        elif current is not None and len(parts) == 1 and parts[0].isdigit():
            out[current].append(parts[0])
    return out


def parse_rocm_pids(raw: str, gpu_map: dict[int, list[str]]) -> dict[int, dict[str, int]]:
    """PID → GPU → bytes for single-card processes.

    rocm-smi reports only a process's COMBINED VRAM when it spans cards;
    duplicating that total onto every card would be false attribution.
    """
    out: dict[int, dict[str, int]] = {}
    for pid, used in _rocm_memory(raw).items():
        cards = gpu_map.get(pid) or []
        if len(cards) == 1:
            out[pid] = {cards[0]: used}
    return out


def parse_nvidia_gpu_map(raw: str) -> dict[str, str]:
    """CSV ``index,uuid`` → UUID → decimal GPU index."""
    out = {}
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit():
            out[parts[1]] = parts[0]
    return out


def parse_nvidia_pids(raw: str, gpu_map: dict[str, str]) -> dict[int, dict[str, int]]:
    """Parse ``--query-compute-apps=pid,gpu_uuid,used_memory`` CSV (MiB)."""
    out: dict[int, dict[str, int]] = {}
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        card = gpu_map.get(parts[1])
        if card is None:
            continue
        try:
            out.setdefault(int(parts[0]), {})[card] = int(float(parts[2]) * 1024**2)
        except ValueError:
            continue
    return out


def _smi_output(command: list[str]) -> tuple[str, str | None]:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"{Path(command[0]).name}: {exc}"
    if proc.returncode:
        detail = proc.stderr.strip() or f"exit {proc.returncode}"
        return "", f"{Path(command[0]).name}: {detail}"
    return proc.stdout, None


def process_vram(config: dict) -> tuple[dict[int, dict[str, int]], str, list[str]]:
    """Best-effort per-PID VRAM from available AMD/NVIDIA management tools."""
    from tools.gpu.status import _resolve

    ctx = cast(ToolContext, SimpleNamespace(config=config))
    by_pid: dict[int, dict[str, int]] = {}
    sources: list[str] = []
    warnings: list[str] = []
    rocm = _resolve("rocm-smi", ctx)
    if rocm:
        raw, error = _smi_output([rocm, "--showpids"])
        if error:
            warnings.append(error)
        else:
            memory = _rocm_memory(raw)
            gpu_map: dict[int, list[str]] = {}
            if memory:
                cards_raw, cards_error = _smi_output(
                    [rocm, "--showpidgpus", *(str(pid) for pid in memory)])
                if cards_error:
                    warnings.append(cards_error)
                else:
                    gpu_map = parse_rocm_pid_gpus(cards_raw)
            by_pid = parse_rocm_pids(raw, gpu_map)
            if any(len(gpu_map.get(pid) or []) > 1 for pid in memory):
                warnings.append("rocm-smi cannot split a multi-GPU process's "
                                "combined VRAM per card")
            sources.append("rocm-smi")
    nvidia = _resolve("nvidia-smi", ctx)
    if nvidia:
        cards_raw, cards_error = _smi_output([
            nvidia, "--query-gpu=index,uuid", "--format=csv,noheader,nounits"])
        apps_raw, apps_error = _smi_output([
            nvidia, "--query-compute-apps=pid,gpu_uuid,used_memory",
            "--format=csv,noheader,nounits"])
        if cards_error or apps_error:
            warnings.extend(e for e in (cards_error, apps_error) if e)
        else:
            for pid, cards in parse_nvidia_pids(
                    apps_raw, parse_nvidia_gpu_map(cards_raw)).items():
                by_pid.setdefault(pid, {}).update(cards)
            sources.append("nvidia-smi")
    if not sources:
        warnings.append("no per-process GPU telemetry available")
    return by_pid, ", ".join(sources), warnings


def _artifact_size(path: Path) -> int:
    """Bytes in one file/tree; never follow symlinks outside the artifact."""
    if path.is_symlink():
        raise OSError("symlinked artifacts are not traversed")
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        raise FileNotFoundError(str(path))
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        base = Path(root)
        dirs[:] = [d for d in dirs if not (base / d).is_symlink()]
        for name in files:
            item = base / name
            if not item.is_symlink():
                total += item.stat().st_size
    return total


def _mount_for(path: Path) -> Path:
    current = path if path.is_dir() else path.parent
    while current.parent != current and not os.path.ismount(current):
        current = current.parent
    return current


def disk_usage(preset_path: str | Path | None) -> dict:
    """Model weights/projector/draft sizes and their filesystems, not caches."""
    conf = parse_preset(preset_path) if preset_path else {}
    artifacts: list[dict] = []
    warnings: list[str] = []
    mounts: dict[str, dict] = {}
    total = 0
    for key, label in (("MODEL_PATH", "model"), ("MMPROJ", "vision projector"),
                       ("MTP_MODEL", "draft model")):
        raw = (conf.get(key) or "").strip()
        if not raw:
            continue
        path = Path(raw)
        row: dict = {"kind": label, "path": str(path), "size_gib": None}
        try:
            size = _artifact_size(path)
            mount = _mount_for(path)
            usage = shutil.disk_usage(mount)
            row["size_gib"] = round(size / _GIB, 2)
            row["mount"] = str(mount)
            total += size
            mounts[str(mount)] = {
                "path": str(mount),
                "total_gib": round(usage.total / _GIB, 2),
                "used_gib": round(usage.used / _GIB, 2),
                "free_gib": round(usage.free / _GIB, 2),
            }
        except OSError as exc:
            row["error"] = str(exc)
            warnings.append(f"{label}: {exc}")
        artifacts.append(row)
    if not artifacts:
        warnings.append("no local model artifacts found in the active preset")
    return {"artifacts": artifacts, "mounts": list(mounts.values()),
            "total_gib": round(total / _GIB, 2), "warnings": warnings}


def collect_usage(root_pid: int, preset: dict, config: dict) -> dict:
    """One consistent, read-only snapshot for the admin process-usage route."""
    from runtime import serving
    from runtime.preset_store import gpu_list

    pids = process_tree(root_pid)
    per_pid, vram_source, warnings = process_vram(config)
    cards = serving.read_vram(SimpleNamespace(config=config)) or []
    card_info = {str(card["index"]): card for card in cards}
    configured = gpu_list(preset)
    attributed: dict[str, int] = {}
    for pid in pids:
        for card, size in (per_pid.get(pid) or {}).items():
            attributed[card] = attributed.get(card, 0) + size
    vram = []
    for card in sorted(
            set(configured) | set(attributed),
            key=lambda value: (not value.isdigit(),
                               int(value) if value.isdigit() else value)):
        info = card_info.get(card) or {}
        card_bytes = attributed.get(card)
        vram.append({"gpu": card,
                     "process_gib": (round(card_bytes / _GIB, 2)
                                     if card_bytes is not None else None),
                     "card_used_gib": info.get("used_gib"),
                     "card_total_gib": info.get("total_gib")})
    # Only this preset's cards are rows. Other GPU tenants contribute to the
    # deliberately whole-card used_gib value, never to process_gib.
    disk = disk_usage(preset.get("preset"))
    return {"pid": root_pid, "ram": ram_usage(pids), "vram": vram,
            "vram_source": vram_source or None, "disk": disk,
            "warnings": warnings + disk["warnings"],
            "collected_at": datetime.now().astimezone().isoformat(timespec="seconds")}
