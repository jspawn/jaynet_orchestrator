"""Update check (Admin → Status → Updates card; GET /api/admin/updates).

Report-only: compares the INSTALLED versions of the external components —
the h5i browser binary, the jevify uv tool, the litellmenv proxy venv, the
llama.cpp server binaries — against upstream (PyPI JSON, GitHub releases)
and returns an upgrade hint per component. Never auto-updates, never
raises: every probe degrades to status "unknown" (or "missing" when the
component isn't installed at all). Outbound calls happen only when an
admin opens/refreshes the card; results cache to DATA/update_check.json
for 24h (refresh=1 bypasses) so the Status tab doesn't hammer upstream.
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
_GITHUB_LLAMA = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
_PYPI = "https://pypi.org/pypi/{}/json"


def _ver_tuple(v: str) -> tuple:
    """Dotted-numeric compare key: 'v0.4.7' → (0,4,7), 'b11282' → (11282,)."""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def _status(installed: str | None, latest: str | None) -> str:
    if not installed:
        return "missing"
    if not latest:
        return "unknown"
    return "behind" if _ver_tuple(installed) < _ver_tuple(latest) else "current"


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


async def _h5i() -> dict:
    out = await asyncio.to_thread(_run_stdout, ["h5i", "--version"])
    installed = out.split()[-1] if out else None
    rel = await _fetch_json(_GITHUB_H5I)
    latest = (rel or {}).get("tag_name")
    return {"component": "h5i (browser plugin)",
            "installed": installed, "latest": latest,
            "status": _status(installed, latest),
            "hint": "re-run the install script: "
                    "curl -fsSL https://h5i.dev/install.sh | sh"}


async def _jevify() -> dict:
    out = await asyncio.to_thread(_run_stdout, ["jevify", "--version"])
    installed = out.split()[-1] if out else None
    meta = await _fetch_json(_PYPI.format("jevify"))
    latest = ((meta or {}).get("info") or {}).get("version")
    return {"component": "jevify (jev plugin sidecar)",
            "installed": installed, "latest": latest,
            "status": _status(installed, latest),
            "hint": "uv tool upgrade jevify"}


async def _litellm() -> dict:
    installed = None
    py = paths.HOME / "litellmenv" / "bin" / "python"
    if py.exists():
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


async def _llama(llama_bins: list[str]) -> dict:
    """Oldest build among the registered llama.cpp binaries is the lagging
    one; per-binary builds land in `detail`. Latest is upstream's newest
    b-tag (the build helper rebuilds against a pin — behind-upstream is
    informational, behind-the-pin is what matters)."""
    builds: dict[str, str] = {}
    for b in dict.fromkeys(llama_bins or []):
        if not b or not Path(b).exists():
            continue
        out = await asyncio.to_thread(_run_stdout, [b, "--version"])
        m = re.search(r"build (\d+)", out or "")
        if m:
            builds[b] = m.group(1)
    installed = min(builds.values(), key=int) if builds else None
    rel = await _fetch_json(_GITHUB_LLAMA)
    latest = ((rel or {}).get("tag_name") or "").lstrip("b") or None
    return {"component": "llama.cpp servers",
            "installed": f"b{installed}" if installed else None,
            "latest": f"b{latest}" if latest else None,
            "status": _status(installed, latest),
            "detail": {b: f"b{v}" for b, v in builds.items()},
            "hint": "run the llama build helper's `update`, then swap or "
                    "restart the model servers to pick up the new binary"}


async def check_updates(config: dict, llama_bins: list[str] | None = None,
                        refresh: bool = False) -> dict:
    """The full report. Cached for 24h unless refresh=True. Never raises."""
    if not refresh:
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if time.time() - float(cached.get("checked_at") or 0) \
                    < CACHE_MAX_AGE_S:
                return cached
        except Exception:
            pass
    components = await asyncio.gather(
        _h5i(), _jevify(), _litellm(), _llama(llama_bins or []))
    out = {"enabled": True, "checked_at": time.time(),
           "checked_at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
           "components": list(components)}
    try:
        CACHE_PATH.write_text(json.dumps(out), encoding="utf-8")
    except Exception as e:
        log.info("update check cache write failed: %s", e)
    return out
