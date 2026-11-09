"""Preset-driven jevify configuration for the plugin's admin API.

A recipe is valid only for the model/deployment it was probed against. Every
configure operation therefore verifies the served model before probing a
temporary recipe and atomically installing it. Nothing here loads or swaps a
model, touches a remote preset, or kills a process JayNet does not manage.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yaml

from runtime import paths
from runtime.preset_store import PresetStore, db_path_for
from runtime.proc import run as proc_run
from runtime.serving import health_now, query_model_ids
from tools.model.catalog import _match_served

_NAME = "jevify"
_PROBE_TIMEOUT_S = 180
_START_TIMEOUT_S = 15
_PORT_RANGE = range(8600, 8611)


class AdminError(Exception):
    """An admin-facing failure with an appropriate HTTP status."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def binary_path() -> str | None:
    """Resolve uv-tool installations even under systemd's reduced PATH."""
    found = shutil.which("jevify")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "jevify"
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _store(config: dict) -> PresetStore:
    return PresetStore(db_path_for(config))


def _dialect(store: PresetStore, preset: dict) -> str:
    return {"llama": "llamacpp", "vllm": "vllm"}.get(
        store.binary_type(preset), "auto")


def _endpoint(preset: dict) -> str:
    return f"http://127.0.0.1:{preset.get('port') or 8080}"


def _local_presets(config: dict) -> tuple[list[dict], dict, PresetStore]:
    store = _store(config)
    rows, slots = store.list_full()
    assigned: dict[str, list[str]] = {}
    for slot, name in slots.items():
        if name:
            assigned.setdefault(name, []).append(slot)
    out = []
    for row in rows:
        # Archived rows remain valid choices while assigned to a live slot —
        # the running brain is often archived after a newer preset is created.
        if ((row.get("archived") and row["name"] not in assigned)
                or (row.get("remote_host") or "").strip()):
            continue
        item = dict(row)
        item["slots"] = assigned.get(row["name"], [])
        item["endpoint"] = _endpoint(row)
        item["dialect"] = _dialect(store, row)
        out.append(item)
    return out, slots, store


async def _running_id(preset: dict) -> str | None:
    try:
        ids = await query_model_ids(preset["endpoint"])
    except Exception:
        return None
    return _match_served(ids, preset)


async def preset_options(config: dict) -> list[dict]:
    """Unarchived local catalog entries, plus their actual served-model state."""
    rows, _, _ = _local_presets(config)
    running = await asyncio.gather(*(_running_id(row) for row in rows))
    return [{
        "name": row["name"],
        "alias": row.get("alias") or row["name"],
        "slots": row["slots"],
        "backend": row["dialect"],
        "endpoint": row["endpoint"],
        "running": served is not None,
        "served_id": served,
    } for row, served in zip(rows, running, strict=True)]


async def selected_preset(config: dict, name: str) -> tuple[dict, str, str]:
    """Return (preset, verified served id, dialect), or an admin-safe error."""
    rows, _, _ = _local_presets(config)
    preset = next((row for row in rows if row["name"] == name), None)
    if preset is None:
        raise AdminError(404, f"unknown or unsupported local preset: {name}")
    served = await _running_id(preset)
    if served is None:
        raise AdminError(
            409, f"{name} is not running at {preset['endpoint']}, or the port "
                 "is serving a different model. Start that preset first; "
                 "JayNet will not swap models for jevify.")
    return preset, served, preset["dialect"]


def _recipe_path(name: str, dialect: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", name).strip(".-") or "preset"
    return paths.DATA / "jevify" / f"{safe}.{dialect}.yaml"


def _recipe_matches(data: dict | None, model: str, endpoint: str,
                    dialect: str) -> bool:
    return (isinstance(data, dict)
            and (data.get("model") or {}).get("name") == model
            and (data.get("endpoint") or {}).get("base_url") == endpoint + "/v1"
            and (data.get("endpoint") or {}).get("dialect") == dialect)


def _new_recipe(model: str, endpoint: str, dialect: str, preset: str) -> dict:
    template = Path(__file__).resolve().parent / "jevify-recipe.example.yaml"
    data = yaml.safe_load(template.read_text(encoding="utf-8"))
    data["model"]["name"] = model
    data["model"]["revision"] = f"JayNet preset: {preset}"
    data["endpoint"]["base_url"] = endpoint + "/v1"
    data["endpoint"]["dialect"] = dialect
    return data


async def prepare_recipe(preset: dict, served: str, dialect: str,
                         reprobe: bool = False) -> tuple[Path, bool]:
    """Return (final recipe, probed-now), preserving the old file on failure."""
    name = preset["name"]
    endpoint = preset["endpoint"]
    final = _recipe_path(name, dialect)
    existing = None
    if final.is_file():
        try:
            existing = yaml.safe_load(final.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            existing = None
    matching = existing if _recipe_matches(
        existing, served, endpoint, dialect) else None
    if (not reprobe and matching is not None
            and isinstance(matching.get("probe"), dict) and matching["probe"]):
        return final, False

    binary = binary_path()
    if binary is None:
        raise AdminError(503, "jevify is not installed (uv tool install jevify)")
    data = matching if matching is not None else _new_recipe(
        served, endpoint, dialect, name)
    final.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{final.name}.", suffix=".tmp",
                                    dir=final.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
        try:
            rc, out, err = await proc_run(
                [binary, "probe", str(tmp)], timeout=_PROBE_TIMEOUT_S)
        except TimeoutError as e:
            raise AdminError(
                504, f"jevify probe timed out after {_PROBE_TIMEOUT_S}s") from e
        if rc != 0:
            tail = (err or out).decode("utf-8", "replace").strip()[-2000:]
            raise AdminError(422, "jevify probe failed; the previous configuration "
                                  f"was left untouched.\n{tail}")
        probed = yaml.safe_load(tmp.read_text(encoding="utf-8"))
        if not isinstance(probed, dict) \
                or not _recipe_matches(probed, served, endpoint, dialect) \
                or not isinstance(probed.get("probe"), dict) or not probed["probe"]:
            raise AdminError(422, "jevify probe returned an incomplete recipe; "
                                  "the previous configuration was left untouched")
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return final, True


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def choose_sidecar_port(mgr, current_url: str = "") -> int:
    """Keep our own managed port; otherwise choose a free loopback port."""
    current = urlparse(current_url or "").port
    if (current is not None and mgr is not None
            and "jevify" in mgr.names() and current in _PORT_RANGE):
        return current
    for port in _PORT_RANGE:
        if _port_free(port):
            return port
    raise AdminError(409, "no free jevify sidecar port in 8600–8610")


async def restart_sidecar(mgr, recipe: Path, base_url: str) -> dict:
    if mgr is None:
        raise AdminError(503, "JayNet's process manager is unavailable")
    binary = binary_path()
    if binary is None:
        raise AdminError(503, "jevify is not installed (uv tool install jevify)")
    port = urlparse(base_url).port
    await mgr.stop_one(_NAME)
    command = shlex.join([binary, "serve", str(recipe), "--port", str(port)])
    mgr.add(_NAME, command, restart=True, restart_delay=10, kill_signal=15)
    await mgr.start_one(_NAME)
    healthy = False
    for _ in range(_START_TIMEOUT_S * 2):
        healthy = (await health_now(base_url)).get("ok", False)
        if healthy:
            break
        await asyncio.sleep(0.5)
    if not healthy:
        logs = "\n".join(mgr.logs(_NAME, 20))[-2000:]
        raise AdminError(502, "jevify did not become healthy on "
                              f"{base_url}.\n{logs}")
    return mgr.status()[_NAME]
