"""Archive tools — safely extract and create .tar.gz/.tgz/.tar/.zip archives.

Confined to the SAME boundary as the fs tools (tools.fs.allowed_roots): both the
archive and the destination must live under an allowed root. Extraction is
hardened against the classic archive attacks:

  * path traversal / absolute members ("zip slip") — every member must resolve
    to a path inside the destination, or the whole operation is refused;
  * symlinks / hardlinks / device & special files — refused (regular files and
    directories only);
  * decompression bombs — per-file, total-size and file-count caps (config via
    tools.archives.*), checked before writing a single byte.

Both ops write to disk, so they're private and require confirmation, exactly like
fs.write / fs.edit.
"""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult

# Reuse the fs confinement so there is ONE allowed-roots policy on the box.
from tools.fs.ops import _resolve

# Caps (overridable via config: tools.archives.{max_files,max_total_bytes,max_file_bytes})
_DEF_MAX_FILES = 5000
_DEF_MAX_TOTAL = 2 * 1024**3      # 2 GiB uncompressed
_DEF_MAX_FILE = 1 * 1024**3       # 1 GiB per member
_SKIP = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".DS_Store"}
_MANIFEST_CAP = 200               # entries listed back to the model


def _caps(ctx: ToolContext) -> tuple[int, int, int]:
    c = ctx.config.get("tools", {}).get("archives", {}) or {}
    return (int(c.get("max_files", _DEF_MAX_FILES)),
            int(c.get("max_total_bytes", _DEF_MAX_TOTAL)),
            int(c.get("max_file_bytes", _DEF_MAX_FILE)))


def _within(dest: Path, name: str) -> Path:
    """Resolve a member name against dest and refuse anything that escapes it."""
    target = (dest / name).resolve()
    if target != dest and dest not in target.parents:
        raise PermissionError(f"unsafe archive member escapes destination: {name!r}")
    return target


class ArchivesExtract(Tool):
    name = "archives.extract"
    description = (
        "Safely extract a .tar.gz/.tgz/.tar/.zip archive into a directory. Both the "
        "archive and the destination must be under an allowed root. Refuses path "
        "traversal, symlinks and oversized/too-many-file bombs. Returns a bounded "
        "manifest of what was written — list a big archive first and extract only "
        "what you need.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "archive": {"type": "string", "description": "Path to the archive file."},
            "dest": {"type": "string",
                     "description": "Destination directory. Default: a folder named "
                                    "after the archive, beside it."},
        },
        "required": ["archive"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            src = _resolve(ctx, args["archive"])
            if not src.is_file():
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=f"not a file: {src}")
            if "dest" in args and args["dest"]:
                dest = _resolve(ctx, args["dest"], must_exist=False)
            else:
                stem = src.name
                for suf in (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar", ".zip"):
                    if stem.lower().endswith(suf):
                        stem = stem[: -len(suf)]; break
                dest = _resolve(ctx, str(src.parent / stem), must_exist=False)
        except (PermissionError, FileNotFoundError, KeyError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))

        max_files, max_total, max_file = _caps(ctx)
        dest_resolved = dest.resolve()
        try:
            if zipfile.is_zipfile(src):
                written = self._extract_zip(src, dest_resolved, max_files, max_total, max_file)
            elif tarfile.is_tarfile(src):
                written = self._extract_tar(src, dest_resolved, max_files, max_total, max_file)
            else:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error="unsupported or corrupt archive (need .zip or a .tar* variant)")
        except (PermissionError, ValueError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))

        total = sum(w[1] for w in written)
        listing = [{"path": p, "bytes": n} for p, n in written[:_MANIFEST_CAP]]
        more = max(0, len(written) - _MANIFEST_CAP)
        return ToolResult(status="ok", tool_name=self.name, result={
            "extracted_to": str(dest_resolved), "files": len(written),
            "total_bytes": total, "manifest": listing,
            "manifest_truncated": more, "note": f"and {more} more" if more else None})

    def _extract_tar(self, src, dest, max_files, max_total, max_file):
        with tarfile.open(src, "r:*") as tar:
            members = tar.getmembers()
            files = [m for m in members if not m.isdir()]
            if len(files) > max_files:
                raise ValueError(f"archive has {len(files)} files (cap {max_files})")
            total = 0
            for m in members:
                if m.issym() or m.islnk():
                    raise PermissionError(f"symlink/hardlink not allowed: {m.name!r}")
                if not (m.isfile() or m.isdir()):
                    raise PermissionError(f"special file not allowed: {m.name!r}")
                _within(dest, m.name)
                if m.isfile():
                    if m.size > max_file:
                        raise ValueError(f"member too large: {m.name!r} ({m.size} > {max_file})")
                    total += m.size
                    if total > max_total:
                        raise ValueError(f"uncompressed size exceeds cap ({max_total})")
            dest.mkdir(parents=True, exist_ok=True)
            written = []
            for m in members:
                target = _within(dest, m.name)
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                fsrc = tar.extractfile(m)
                if fsrc is None:
                    continue
                with open(target, "wb") as out:
                    shutil.copyfileobj(fsrc, out, length=1024 * 1024)
                written.append((str(target.relative_to(dest)), m.size))
            return written

    def _extract_zip(self, src, dest, max_files, max_total, max_file):
        with zipfile.ZipFile(src) as zf:
            infos = zf.infolist()
            real = [zi for zi in infos if not zi.is_dir()]
            if len(real) > max_files:
                raise ValueError(f"archive has {len(real)} files (cap {max_files})")
            total = 0
            for zi in infos:
                _within(dest, zi.filename)
                mode = zi.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    raise PermissionError(f"symlink not allowed: {zi.filename!r}")
                if not zi.is_dir():
                    if zi.file_size > max_file:
                        raise ValueError(f"member too large: {zi.filename!r} ({zi.file_size} > {max_file})")
                    total += zi.file_size
                    if total > max_total:
                        raise ValueError(f"uncompressed size exceeds cap ({max_total})")
            dest.mkdir(parents=True, exist_ok=True)
            written = []
            for zi in infos:
                target = _within(dest, zi.filename)
                if zi.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(zi) as fsrc, open(target, "wb") as out:
                    shutil.copyfileobj(fsrc, out, length=1024 * 1024)
                written.append((str(target.relative_to(dest)), zi.file_size))
            return written


_FORMATS = {"tar.gz": "w:gz", "tgz": "w:gz", "tar.bz2": "w:bz2",
            "tar.xz": "w:xz", "tar": "w:", "zip": "zip"}


class ArchivesCreate(Tool):
    name = "archives.create"
    description = (
        "Create an archive from one or more files/directories. format: tar.gz "
        "(default), tgz, tar, tar.bz2, tar.xz, or zip. All sources and the output "
        "must be under an allowed root. Junk dirs (.git, __pycache__, .venv, "
        "node_modules) are skipped. Returns the output path and a size summary.")
    private = True
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Files/dirs to include."},
            "output": {"type": "string", "description": "Archive path to write."},
            "format": {"type": "string", "enum": sorted(_FORMATS),
                       "description": "Archive format. Default tar.gz."},
        },
        "required": ["paths", "output"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        fmt = (args.get("format") or "tar.gz").lower()
        if fmt not in _FORMATS:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"unknown format {fmt!r}; use one of {sorted(_FORMATS)}")
        try:
            srcs = [_resolve(ctx, p) for p in (args.get("paths") or [])]
            if not srcs:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error="no input paths")
            out = _resolve(ctx, args["output"], must_exist=False)
        except (PermissionError, FileNotFoundError, KeyError) as e:
            return ToolResult(status="error", result=None, tool_name=self.name, error=str(e))

        max_files, max_total, max_file = _caps(ctx)
        out.parent.mkdir(parents=True, exist_ok=True)

        def _iter(root: Path):
            if root.is_file():
                yield root, root.name; return
            base = root.parent
            for dpath, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in _SKIP]
                for f in files:
                    if f in _SKIP:
                        continue
                    fp = Path(dpath) / f
                    if fp.is_symlink():
                        continue
                    yield fp, str(fp.relative_to(base))

        members = []
        total = 0
        for s in srcs:
            for fp, arc in _iter(s):
                sz = fp.stat().st_size
                if sz > max_file:
                    return ToolResult(status="error", result=None, tool_name=self.name,
                                      error=f"input too large: {fp} ({sz} > {max_file})")
                total += sz
                if len(members) >= max_files:
                    return ToolResult(status="error", result=None, tool_name=self.name,
                                      error=f"too many files (cap {max_files})")
                if total > max_total:
                    return ToolResult(status="error", result=None, tool_name=self.name,
                                      error=f"total size exceeds cap ({max_total})")
                members.append((fp, arc))

        try:
            if fmt == "zip":
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fp, arc in members:
                        zf.write(fp, arcname=arc)
            else:
                with tarfile.open(out, _FORMATS[fmt]) as tar:
                    for fp, arc in members:
                        tar.add(fp, arcname=arc, recursive=False)
        except Exception as e:   # noqa: BLE001 — surface any archive write failure cleanly
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"failed to write archive: {e}")

        comp = out.stat().st_size if out.exists() else 0
        return ToolResult(status="ok", tool_name=self.name, result={
            "output": str(out), "format": fmt, "files": len(members),
            "input_bytes": total, "archive_bytes": comp})
