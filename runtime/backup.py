"""Data-dir backup: consistent snapshots of the SQLite stores (via the online
backup API, so no live -wal/-shm is copied) plus the small non-db state worth
keeping, packed as a tar.gz.

Used by two callers:
- GET /api/admin/backup (web/routes_admin.py) — the interactive download;
- the scheduled/off-console path: `python -m runtime.backup` from
  scripts/backup.sh + systemd/jaynet-backup.timer (readiness audit OPS-2:
  backups were manual-only).

The recovery set deliberately EXCLUDES secrets (jaynet.env, unit files) —
see docs/operations.md for the full bare-metal rebuild list.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

BACKUP_DIRS = ("wiki", "custom", "uploads", "projects", "presets")
BACKUP_FILES = ("budget-defaults.json", "schedules.json", "litellm.yaml")
BACKUP_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "tmp", ".cache")


def snapshot_db(src: Path, dst: Path) -> None:
    sconn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dconn = sqlite3.connect(str(dst))
    try:
        sconn.backup(dconn)
    finally:
        dconn.close()
        sconn.close()


def build_archive(data_dir: Path, work_dir: Path, stamp: str | None = None) -> Path:
    """Snapshot data_dir into work_dir and return the tar.gz path."""
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    stage = work_dir / "data"
    stage.mkdir(parents=True, exist_ok=True)
    for db in sorted(data_dir.glob("*.db")):
        snapshot_db(db, stage / db.name)
    for d in BACKUP_DIRS:
        src = data_dir / d
        if src.is_dir():
            shutil.copytree(src, stage / d, ignore=BACKUP_IGNORE)
    for f in BACKUP_FILES:
        src = data_dir / f
        if src.is_file():
            shutil.copy2(src, stage / f)
    out = work_dir / f"jaynet-backup-{stamp}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for item in sorted(stage.iterdir()):
            tar.add(item, arcname=item.name)
    return out


def main(argv: list[str]) -> int:
    """CLI: `python -m runtime.backup [out_dir]` — one archive, kept (not
    streamed+deleted like the admin download). Path-agnostic via JAYNET_DATA
    (falls back to runtime.paths' default)."""
    from runtime import paths
    out_dir = Path(argv[1]) if len(argv) > 1 else Path("/srv/backups")
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="jaynet-backup-"))
    try:
        out = build_archive(paths.DATA, tmp)
        final = out_dir / out.name
        shutil.move(str(out), final)
        final.chmod(0o600)
        print(f"[backup] wrote {final} ({final.stat().st_size / 1e6:.1f} MB)")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
