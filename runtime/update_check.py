"""Update check (Admin → Status & Usage → Overview; GET /api/admin/updates).

Report-only: compares active/installed components — h5i, jevify, litellmenv,
and llama.cpp/vLLM/radiance server binaries — against upstream (PyPI JSON,
GitHub/Codeberg tags) and returns an upgrade hint per component. Missing,
inactive components are omitted; an active plugin with a missing binary gets
a "missing" row. Never auto-updates, never raises: failed probes degrade to
"unknown". Outbound calls happen only when an admin opens/refreshes the card;
results cache per availability scope to DATA/update_check.json for 24h
(refresh=1 bypasses) so the Status tab doesn't hammer upstream.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from pathlib import Path

import httpx

from runtime import paths

log = logging.getLogger("update_check")

CACHE_PATH = paths.DATA / "update_check.json"
CACHE_MAX_AGE_S = 24 * 3600

_GITHUB_H5I = "https://api.github.com/repos/h5i-dev/h5i/releases/latest"
# NOT /releases/latest: llama.cpp's latest endpoint can point at an
# asset-less or non-b tag (same reason quickstart walks the list) — the
# update check takes the newest b#### tag from the recent releases.
_GITHUB_LLAMA = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15"
_CODEBERG_RADIANCE = "https://codeberg.org/api/v1/repos/StillDeadcode/radiance/tags"
_PYPI = "https://pypi.org/pypi/{}/json"


def _ver_tuple(v: str) -> tuple:
    """Dotted-numeric compare key: 'v0.4.7' → (0,4,7), 'b11282' → (11282,)."""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def _status(installed: str | None, latest: str | None) -> str:
    if not installed:
        return "missing"
    if not latest:
        return "unknown"
    inst, lat = _ver_tuple(installed), _ver_tuple(latest)
    # Different SHAPES (e.g. build tag (11280,) vs release tag (0,5,0)) can't
    # be ordered meaningfully — comparing them silently inverts the answer
    # (live: llama "current" while 40 builds behind, audit #26 D7). Degrade
    # to "unknown" instead of guessing.
    if not inst or not lat or len(inst) != len(lat):
        return "unknown"
    return "behind" if inst < lat else "current"


# --- probe seams (module-level so tests monkeypatch these, never the network) ---

async def _fetch_json(url: str) -> dict | None:
    try:
        async with httpx.AsyncClient(
                timeout=5.0,
                headers={"User-Agent": "jaynet-update-check"}) as c:
            r = await c.get(url)
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        log.info("update check fetch %s failed: %s", url, e)
    return None


def _run_stdout(cmd: list[str], timeout: int = 15) -> str | None:
    """Sync subprocess probe — always invoked via asyncio.to_thread. None on
    any failure (missing binary, timeout, nonzero output is still parsed:
    several tools print their version to stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or p.stderr).strip() or None
    except Exception:
        return None


async def _version_of(name: str) -> str | None:
    """<name> --version with PATH fallbacks: uv tool installs land in
    ~/.local/bin and manual ones in /usr/local/bin — the systemd service
    PATH does not necessarily carry either (live: jevify showed 'missing'
    while installed)."""
    import os
    for exe in (name, f"/usr/local/bin/{name}",
                os.path.expanduser(f"~/.local/bin/{name}")):
        out = await asyncio.to_thread(_run_stdout, [exe, "--version"])
        if out:
            return out.split()[-1]
    return None


def _existing(bins: list[str] | None) -> list[str]:
    return [b for b in dict.fromkeys(bins or []) if b and Path(b).exists()]


def _release_key(v: str) -> tuple:
    """Three-part release key, ignoring rc/dev/git-describe suffixes."""
    return _ver_tuple(v)[:3]


def _release_status(installed: str | None, latest: str | None) -> str:
    if not installed:
        return "missing"
    if not latest:
        return "unknown"
    inst, lat = _release_key(installed), _release_key(latest)
    if len(inst) != 3 or len(lat) != 3:
        return "unknown"
    return "behind" if inst < lat else "current"


async def _h5i(installed: str | None) -> dict:
    rel = await _fetch_json(_GITHUB_H5I)
    latest = (rel or {}).get("tag_name")
    return {"component": "h5i (browser plugin)",
            "installed": installed, "latest": latest,
            "status": _status(installed, latest),
            "hint": "re-run the install script: "
                    "curl -fsSL https://h5i.dev/install.sh | sh"}


async def _jevify(installed: str | None) -> dict:
    meta = await _fetch_json(_PYPI.format("jevify"))
    latest = ((meta or {}).get("info") or {}).get("version")
    return {"component": "jevify (jev plugin sidecar)",
            "installed": installed, "latest": latest,
            "status": _status(installed, latest),
            "hint": "uv tool upgrade jevify"}


async def _litellm() -> dict | None:
    py = paths.HOME / "litellmenv" / "bin" / "python"
    if not py.exists():
        return None
    out = await asyncio.to_thread(
        _run_stdout, [str(py), "-c",
                      "import importlib.metadata as m; "
                      "print(m.version('litellm'))"])
    installed = out if out and re.match(r"^\d", out) else None
    pinned = None
    lock = paths.HOME / "requirements-litellm.lock"
    if lock.exists():
        m = re.search(r"^litellm==([\d.]+)",
                      lock.read_text(encoding="utf-8"), re.M)
        pinned = m.group(1) if m else None
    meta = await _fetch_json(_PYPI.format("litellm"))
    latest = ((meta or {}).get("info") or {}).get("version")
    # The lock pins the TESTED set, so status is measured against the PIN,
    # not PyPI: running behind the lock is the real "behind" (live drifted
    # to 1.87.0 while the lock said 1.102.1); a newer PyPI release than the
    # pin is informational only. Without a pin, fall back to latest.
    if installed and pinned:
        status = "behind" if _ver_tuple(installed) < _ver_tuple(pinned) \
            else "current"
    else:
        status = _status(installed, latest)
    return {"component": "litellm proxy (litellmenv)",
            "installed": installed, "latest": latest, "pinned": pinned,
            "status": status,
            "hint": "lock-pinned; re-sync: uv pip install --python "
                    "litellmenv/bin/python -r requirements-litellm.lock, "
                    "then restart litellm-proxy"}


async def _llama(llama_bins: list[str]) -> dict | None:
    """Oldest build among the registered llama.cpp binaries is the lagging
    one; per-binary builds land in `detail`. Latest is upstream's newest
    b-tag (the build helper rebuilds against a pin — behind-upstream is
    informational, behind-the-pin is what matters)."""
    bins = _existing(llama_bins)
    if not bins:
        return None
    builds: dict[str, str] = {}
    for b in bins:
        out = await asyncio.to_thread(_run_stdout, [b, "--version"])
        m = re.search(r"build (\d+)", out or "")
        if m:
            builds[b] = m.group(1)
    installed = min(builds.values(), key=int) if builds else None
    rel = await _fetch_json(_GITHUB_LLAMA)
    latest = None
    releases: list = rel if isinstance(rel, list) else []
    for r in releases:
        tag = str(r.get("tag_name") or "")
        if re.fullmatch(r"b\d+", tag):
            latest = tag.lstrip("b")
            break
    return {"component": "llama.cpp servers",
            "installed": f"b{installed}" if installed else None,
            "latest": f"b{latest}" if latest else None,
            "status": _status(installed, latest),
            "detail": {b: f"b{v}" for b, v in builds.items()},
            "hint": "run the llama build helper's `update`, then swap or "
                    "restart the model servers to pick up the new binary"}


async def _vllm(vllm_bins: list[str]) -> dict | None:
    bins = _existing(vllm_bins)
    if not bins:
        return None
    builds: dict[str, str] = {}
    for b in bins:
        out = await asyncio.to_thread(_run_stdout, [b, "--version"])
        m = re.search(r"\d+\.\d+\.\d+[A-Za-z0-9.+_-]*", out or "")
        if m:
            builds[b] = m.group(0)
    installed = min(builds.values(), key=_release_key) if builds else None
    meta = await _fetch_json(_PYPI.format("vllm"))
    latest = ((meta or {}).get("info") or {}).get("version")
    return {"component": "vLLM servers",
            "installed": installed, "latest": latest,
            "status": _release_status(installed, latest), "detail": builds,
            "hint": "run scripts/build_tools.sh vllm rocm, then restart the "
                    "vLLM model servers"}


async def _radiance(radiance_bins: list[str]) -> dict | None:
    bins = _existing(radiance_bins)
    if not bins:
        return None
    # radiance intentionally has no --version flag. The build helper installs
    # radiance.rocm-<git describe> and points the stable registry path at it.
    builds: dict[str, str] = {}
    for b in bins:
        resolved = str(Path(b).resolve())
        m = re.search(r"radiance\.[^/]+-(v\d+\.\d+\.\d+"
                      r"(?:-\d+-g[0-9a-f]+)?)", resolved)
        if m:
            builds[b] = m.group(1)
    installed = min(builds.values(), key=_release_key) if builds else None
    rel = await _fetch_json(_CODEBERG_RADIANCE)
    tags: list = rel if isinstance(rel, list) else []
    versions = [str(r.get("name") or "") for r in tags]
    versions = [v for v in versions if re.fullmatch(r"v\d+\.\d+\.\d+", v)]
    latest = max(versions, key=_release_key) if versions else None
    return {"component": "radiance servers",
            "installed": installed, "latest": latest,
            "status": _release_status(installed, latest), "detail": builds,
            "hint": "run scripts/build_tools.sh radiance rocm, then restart "
                    "the radiance model servers"}


async def check_updates(llama_bins: list[str] | None = None,
                        refresh: bool = False, *,
                        vllm_bins: list[str] | None = None,
                        radiance_bins: list[str] | None = None,
                        h5i_active: bool = False, jev_active: bool = False) -> dict:
    """Installed components only, cached per availability scope for 24h."""
    h5i_installed, jev_installed = await asyncio.gather(
        _version_of("h5i"), _version_of("jevify"))
    llama_paths = _existing(llama_bins)
    vllm_paths = _existing(vllm_bins)
    radiance_paths = _existing(radiance_bins)
    scope = {"llama": llama_paths, "vllm": vllm_paths,
             "radiance": radiance_paths,
             "litellm_installed": (paths.HOME / "litellmenv" / "bin" / "python").exists(),
             "h5i_active": h5i_active, "jev_active": jev_active,
             "h5i_installed": h5i_installed, "jev_installed": jev_installed}
    if not refresh:
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if cached.get("scope") == scope and \
                    time.time() - float(cached.get("checked_at") or 0) \
                    < CACHE_MAX_AGE_S:
                return cached
        except Exception:
            pass
    probes: list = []
    if h5i_active or h5i_installed:
        probes.append(_h5i(h5i_installed))
    if jev_active or jev_installed:
        probes.append(_jevify(jev_installed))
    probes += [_litellm(), _llama(llama_paths), _vllm(vllm_paths),
               _radiance(radiance_paths)]
    components = [c for c in await asyncio.gather(*probes) if c is not None]
    out = {"enabled": True, "checked_at": time.time(),
           "checked_at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
           "scope": scope, "components": components}
    try:
        CACHE_PATH.write_text(json.dumps(out), encoding="utf-8")
    except Exception as e:
        log.info("update check cache write failed: %s", e)
    return out
