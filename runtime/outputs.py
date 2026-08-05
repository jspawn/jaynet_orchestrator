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
import logging
import os
import shutil
import tarfile
import time
from pathlib import Path

log = logging.getLogger(__name__)


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


def is_safe_run_id(run_id: object) -> bool:
    """True only when run_id is a single, plain path component.

    run_id reaches this module from client-controlled saved-chat turns, so it
    must never be more than one relative name: no separators, no dot segments,
    nothing absolute. This is the containment check that keeps every outputs
    operation inside outputs_root."""
    if not isinstance(run_id, str) or not run_id or "\x00" in run_id:
        return False
    p = Path(run_id)
    return (not p.is_absolute() and len(p.parts) == 1
            and p.parts[0] not in (".", "..") and "\\" not in run_id)


def _run_dir(outputs_root: str | Path, run_id: str) -> Path | None:
    """The run's staging dir, or None when run_id isn't a safe path component."""
    if not is_safe_run_id(run_id):
        return None
    return Path(outputs_root) / run_id


def _copy_tree(src: Path, dest: Path) -> None:
    """Copy a directory tree preserving symlinks, but skip any symlink whose
    resolved target escapes the source root (copytree's default symlinks=False
    would silently dereference it — e.g. link -> /etc/shadow would leak the
    target's bytes into the delivered tarball)."""
    root = src.resolve()

    def ignore(directory: str, names: list[str]) -> list[str]:
        skipped = []
        for n in names:
            p = Path(directory) / n
            if not p.is_symlink():
                continue
            try:
                target = p.resolve()
            except OSError:
                target = root  # unresolvable -> treat as inside, copied as-is
            if target != root and root not in target.parents:
                log.warning("deliver.files: skipping symlink escaping the source "
                            "root: %s -> %s", p, os.readlink(p))
                skipped.append(n)
        return skipped

    shutil.copytree(src, dest, symlinks=True, ignore=ignore)


def stage_and_bundle(outputs_root: str | Path, run_id: str, owner: str | None,
                     paths: list[str], suggested_name: str | None,
                     max_bytes: int) -> dict:
    """Copy each path into the run's staging dir, then (re)build a single
    deliverable across everything staged so far. Returns the manifest dict.

    One file, nothing else -> served as that file. Anything else (>1 entry, or a
    folder) -> a single .tar.gz. Accumulates across calls within a run.
    """
    rundir = _run_dir(outputs_root, run_id)
    if rundir is None:
        raise ValueError(f"unsafe run_id: {run_id!r}")
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
            _copy_tree(src, dest)
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
    rundir = _run_dir(outputs_root, run_id)
    if rundir is None:
        return None
    mp = rundir / "manifest.json"
    if not mp.is_file():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def deliverable_path(outputs_root: str | Path, run_id: str, manifest: dict) -> Path:
    rundir = _run_dir(outputs_root, run_id)
    if rundir is None:
        raise ValueError(f"unsafe run_id: {run_id!r}")
    if manifest.get("kind") == "file":
        return rundir / "files" / manifest["name"]
    return rundir / "delivery.tar.gz"


def _owner_matches(m: dict | None, owner: str | None) -> bool:
    """True when the caller may touch this output. owner=None means the caller
    didn't authenticate as a user (token/CLI path) — no check. A missing
    manifest means an orphan staging dir with no owner to protect — allowed.
    Otherwise the manifest's owner must match: a run_id embedded in a saved
    chat is client-supplied, so without this a user could mark/delete another
    user's delivered files."""
    if owner is None or m is None:
        return True
    return m.get("owner") == owner


def mark_saved(outputs_root: str | Path, run_id: str, saved: bool = True,
               owner: str | None = None) -> None:
    rundir = _run_dir(outputs_root, run_id)
    if rundir is None:
        return
    m = read_manifest(outputs_root, run_id)
    if m is None or not _owner_matches(m, owner):
        return
    m["saved"] = saved
    (rundir / "manifest.json").write_text(
        json.dumps(m), encoding="utf-8")


def delete_output(outputs_root: str | Path, run_id: str,
                  owner: str | None = None) -> None:
    rundir = _run_dir(outputs_root, run_id)
    if rundir is None:
        return
    if not _owner_matches(read_manifest(outputs_root, run_id), owner):
        return
    shutil.rmtree(rundir, ignore_errors=True)


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


def _newest_mtime(d: Path) -> float:
    """Most-recent mtime anywhere under `d` (the dir itself if empty)."""
    try:
        newest = d.stat().st_mtime
    except OSError:
        return 0.0
    for p in d.rglob("*"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m > newest:
            newest = m
    return newest


def sweep_scratch(scratch_root: str | Path, ttl_hours: float) -> int:
    """Remove per-chat scratch workspaces untouched for longer than the TTL.

    Layout: <scratch_root>/<owner>/<cid>/files/...  Each <cid> dir is aged by the
    most-recent mtime anywhere beneath it, so a chat that's still being worked in
    is spared while abandoned ones are reclaimed. Returns how many were removed.
    """
    root = Path(scratch_root)
    if not root.is_dir():
        return 0
    cutoff = time.time() - ttl_hours * 3600
    removed = 0
    for owner_dir in root.iterdir():
        if not owner_dir.is_dir():
            continue
        for cid_dir in owner_dir.iterdir():
            if cid_dir.is_dir() and _newest_mtime(cid_dir) < cutoff:
                shutil.rmtree(cid_dir, ignore_errors=True)
                removed += 1
        try:                                  # drop a now-empty owner dir
            next(owner_dir.iterdir())
        except StopIteration:
            try:
                owner_dir.rmdir()
            except OSError:
                pass
        except OSError:
            pass
    return removed
