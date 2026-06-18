"""File delivery — the server→user side of the web client.

A run can hand files back to the user via the `deliver.files` tool. The bytes are
staged under `<outputs_root>/<run_id>/files/` and turned into a single deliverable:
a lone file is served as-is; multiple files (or any folder) are bundled into one
`.tar.gz`. A `manifest.json` records the owner, kind, name, size, and a `saved`
flag.

Lifecycle: outputs are ephemeral by default (`saved: false`). Saving the chat that
contains the run flips `saved: true` (see the web layer); deleting the chat removes
the output. A sweep deletes any still-unsaved output older than a grace TTL, so
files survive the active session and any chat the user chooses to keep, but
orphans don't accumulate.

Shared by the `deliver.files` tool (stage/bundle) and the web server (serve, mark,
sweep). Stdlib only.
"""

from __future__ import annotations

import datetime
import json
import shutil
import tarfile
from pathlib import Path


class OutputTooLarge(Exception):
    def __init__(self, size: int, limit: int):
        self.size, self.limit = size, limit
        super().__init__(f"delivery is {size} bytes; limit is {limit}")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _safe_name(name: str) -> str:
    name = (name or "").replace("\\", "/").split("/")[-1]
    out = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in name).strip(". ")
    return out[:120] or "download"


def _tree_size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _run_dir(outputs_root: str | Path, run_id: str) -> Path:
    return Path(outputs_root) / run_id


def stage_and_bundle(outputs_root: str | Path, run_id: str, owner: str | None,
                     paths: list[str], suggested_name: str | None,
                     max_bytes: int) -> dict:
    """Copy each path into the run's staging dir, then (re)build a single
    deliverable across everything staged so far. Returns the manifest dict.

    One file, nothing else -> served as that file. Anything else (>1 entry, or a
    folder) -> a single .tar.gz. Accumulates across calls within a run.
    """
    rundir = _run_dir(outputs_root, run_id)
    files_dir = rundir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    # Resolve + size-check sources before copying (avoid partial copies).
    srcs = []
    for raw in paths:
        src = Path(raw).expanduser()
        try:
            src = src.resolve()
        except OSError:
            pass
        if not src.exists():
            raise FileNotFoundError(raw)
        srcs.append(src)
    prospective = _tree_size(files_dir) + sum(_tree_size(s) for s in srcs)
    if prospective > max_bytes:
        raise OutputTooLarge(prospective, max_bytes)

    for src in srcs:
        dest = files_dir / src.name
        if src.is_dir():
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)

    entries = sorted(files_dir.iterdir())
    targz = rundir / "delivery.tar.gz"
    if targz.exists():
        targz.unlink()

    if len(entries) == 1 and entries[0].is_file():
        kind, name, deliverable = "file", entries[0].name, entries[0]
    else:
        kind = "targz"
        name = _safe_name(suggested_name or "delivery.tar.gz")
        if not name.endswith(".tar.gz"):
            name += ".tar.gz"
        with tarfile.open(targz, "w:gz") as tar:
            for e in entries:
                tar.add(e, arcname=e.name)
        deliverable = targz

    size = deliverable.stat().st_size
    if size > max_bytes:
        raise OutputTooLarge(size, max_bytes)

    manifest = {
        "owner": owner, "run_id": run_id, "kind": kind, "name": name,
        "size": size, "created_at": _now_iso(), "saved": False,
    }
    (rundir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def read_manifest(outputs_root: str | Path, run_id: str) -> dict | None:
    mp = _run_dir(outputs_root, run_id) / "manifest.json"
    if not mp.is_file():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def deliverable_path(outputs_root: str | Path, run_id: str, manifest: dict) -> Path:
    rundir = _run_dir(outputs_root, run_id)
    if manifest.get("kind") == "file":
        return rundir / "files" / manifest["name"]
    return rundir / "delivery.tar.gz"


def mark_saved(outputs_root: str | Path, run_id: str, saved: bool = True) -> None:
    m = read_manifest(outputs_root, run_id)
    if m is None:
        return
    m["saved"] = saved
    (_run_dir(outputs_root, run_id) / "manifest.json").write_text(
        json.dumps(m), encoding="utf-8")


def delete_output(outputs_root: str | Path, run_id: str) -> None:
    shutil.rmtree(_run_dir(outputs_root, run_id), ignore_errors=True)


def sweep(outputs_root: str | Path, ttl_hours: float) -> int:
    """Delete unsaved outputs older than the TTL (and manifest-less orphans).
    Returns how many were removed."""
    root = Path(outputs_root)
    if not root.is_dir():
        return 0
    cutoff = datetime.datetime.now(datetime.timezone.utc) - \
        datetime.timedelta(hours=ttl_hours)
    removed = 0
    for rundir in root.iterdir():
        if not rundir.is_dir():
            continue
        m = read_manifest(root, rundir.name)
        if m is None:
            # orphan staging (e.g. crashed mid-run): age by mtime
            try:
                mtime = datetime.datetime.fromtimestamp(
                    rundir.stat().st_mtime, datetime.timezone.utc)
            except OSError:
                continue
            if mtime < cutoff:
                shutil.rmtree(rundir, ignore_errors=True); removed += 1
            continue
        if m.get("saved"):
            continue
        try:
            created = datetime.datetime.fromisoformat(m["created_at"])
        except (KeyError, ValueError):
            created = cutoff  # malformed -> eligible
        if created < cutoff:
            shutil.rmtree(rundir, ignore_errors=True); removed += 1
    return removed
