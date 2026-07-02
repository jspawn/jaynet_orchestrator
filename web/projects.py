"""Projects — named, persistent workspaces the agent and the user share.

A project is just an owner-scoped directory on disk:

    projects_dir/<owner>/<project_id>/
        project.json          # metadata (id, name, created_at)
        files/                # the working tree — what the editor and fs.* both see

Because `files/` lives under the orchestrator data dir (an fs allowed root), the
agent operates on a project with the *existing* `fs.read/write/list/grep` tools —
no project-specific agent tooling needed. The web editor reads/writes the same
files over HTTP, so manual edits and agent edits land in one place.

Stdlib only. Every path that comes from a client is run through `safe_join`, which
resolves it inside the project's `files/` root and refuses anything that escapes
(`..`, absolute paths, symlink hops).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

_SKIP = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".DS_Store"}
_MAX_TREE = 2000          # entries
_TEXT_READ_CAP = 400_000  # bytes served to the editor


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(name: str) -> str:
    out = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (name or "").strip())
    return out.strip("-").lower()[:40] or "project"


def owner_dir(projects_dir: str | Path, owner: str | None) -> Path:
    return Path(projects_dir) / (owner or "_token")


def _proj_dir(projects_dir, owner, pid) -> Path:
    return owner_dir(projects_dir, owner) / pid


def files_root(projects_dir, owner, pid) -> Path | None:
    """The project's working tree, or None if the project doesn't exist."""
    d = _proj_dir(projects_dir, owner, pid)
    return (d / "files") if (d / "project.json").is_file() else None


def read_meta(projects_dir, owner, pid) -> dict | None:
    p = _proj_dir(projects_dir, owner, pid) / "project.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def create_project(projects_dir, owner, name: str) -> dict:
    pid = f"{_slug(name)}-{secrets.token_hex(3)}"
    d = _proj_dir(projects_dir, owner, pid)
    (d / "files").mkdir(parents=True, exist_ok=True)
    meta = {"id": pid, "name": (name or pid).strip()[:120], "created_at": _now()}
    (d / "project.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def list_projects(projects_dir, owner) -> list[dict]:
    base = owner_dir(projects_dir, owner)
    if not base.is_dir():
        return []
    out = []
    for d in base.iterdir():
        m = read_meta(projects_dir, owner, d.name) if d.is_dir() else None
        if m:
            root = d / "files"
            m = {**m, "file_count": sum(1 for _ in _iter_files(root))}
            out.append(m)
    out.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return out


def delete_project(projects_dir, owner, pid) -> bool:
    d = _proj_dir(projects_dir, owner, pid)
    if not (d / "project.json").is_file():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


def safe_join(root: Path, rel: str) -> Path | None:
    """Resolve `rel` inside `root`; None if it escapes (traversal/absolute/symlink)."""
    root = root.resolve()
    p = (root / (rel or "").lstrip("/")).resolve()
    if p == root or root in p.parents:
        return p
    return None


def _iter_files(root: Path):
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP]
        for f in filenames:
            if f not in _SKIP:
                yield Path(dirpath) / f


def tree(root: Path) -> list[dict]:
    """Flat, sorted [{path, type, size}] of the project (dirs and files), relative
    to the project root, POSIX separators."""
    if not root.is_dir():
        return []
    entries: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP)
        rel_dir = Path(dirpath).relative_to(root)
        for d in dirnames:
            rp = (rel_dir / d).as_posix()
            entries.append({"path": rp, "type": "dir", "size": 0})
        for f in sorted(filenames):
            if f in _SKIP:
                continue
            fp = Path(dirpath) / f
            rp = (rel_dir / f).as_posix()
            try:
                size = fp.stat().st_size
            except OSError:
                size = 0
            entries.append({"path": rp, "type": "file", "size": size})
        if len(entries) > _MAX_TREE:
            break
    entries.sort(key=lambda e: e["path"])
    return entries[:_MAX_TREE]


def tree_text(root: Path, max_lines: int = 200) -> str:
    """Compact indented listing for the chat prompt."""
    lines = []
    for e in tree(root):
        depth = e["path"].count("/")
        name = e["path"].rsplit("/", 1)[-1]
        lines.append("  " * depth + name + ("/" if e["type"] == "dir" else ""))
        if len(lines) >= max_lines:
            lines.append("… (truncated)")
            break
    return "\n".join(lines) if lines else "(empty project)"


def read_file(root: Path, rel: str) -> dict | None:
    p = safe_join(root, rel)
    if p is None or not p.is_file():
        return None
    raw = p.read_bytes()
    if b"\x00" in raw[:8192]:
        return {"path": rel, "binary": True, "size": len(raw), "content": None}
    truncated = len(raw) > _TEXT_READ_CAP
    text = raw[:_TEXT_READ_CAP].decode("utf-8", errors="replace")
    return {"path": rel, "binary": False, "size": len(raw),
            "truncated": truncated, "content": text}


def write_file(root: Path, rel: str, content: bytes | str) -> dict | None:
    p = safe_join(root, rel)
    if p is None:
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    p.write_bytes(data)
    return {"path": rel, "size": len(data)}


def delete_path(root: Path, rel: str) -> bool:
    p = safe_join(root, rel)
    if p is None or not p.exists():
        return False
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink()
    return True


def mkdir(root: Path, rel: str) -> dict | None:
    """Create a folder (and any parents). None on an escaping/empty path."""
    rel = (rel or "").strip().strip("/")
    if not rel:
        return None
    p = safe_join(root, rel)
    if p is None:
        return None
    p.mkdir(parents=True, exist_ok=True)
    return {"path": rel, "type": "dir"}


def move_path(root: Path, src: str, dst: str) -> dict | None:
    """Rename/move `src` to `dst` (both project-relative). None if either path
    escapes the root, src is missing, or dst already exists (no silent overwrite)."""
    src = (src or "").strip().strip("/")
    dst = (dst or "").strip().strip("/")
    if not src or not dst:
        return None
    s = safe_join(root, src)
    d = safe_join(root, dst)
    if s is None or d is None or not s.exists() or d.exists():
        return None
    # refuse to move a folder into itself/its own subtree
    if s.is_dir() and (d == s or s in d.parents):
        return None
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return {"from": src, "to": dst}
