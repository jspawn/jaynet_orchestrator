#!/usr/bin/env python3
"""Inspect or extract archives (stdlib only): .zip, .tar, .tar.gz/.tgz, .tar.bz2.

Usage:
  inspect_archive.py <archive>                 # list contents (size + name)
  inspect_archive.py <archive> extract [dest]  # extract, with traversal guard
"""

import os
import sys
import tarfile
import zipfile


def entries(path: str) -> list[tuple[str, int]]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            return [(i.filename, i.file_size) for i in z.infolist() if not i.is_dir()]
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as t:
            return [(m.name, m.size) for m in t.getmembers() if m.isfile()]
    raise ValueError("unsupported or unreadable archive")


def _within(dest: str, name: str) -> bool:
    base = os.path.realpath(dest)
    p = os.path.realpath(os.path.join(dest, name))
    return p == base or p.startswith(base + os.sep)


def extract(path: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if not _within(dest, n):
                    raise ValueError(f"unsafe path in archive: {n}")
            z.extractall(dest)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as t:
            for m in t.getmembers():
                if not _within(dest, m.name):
                    raise ValueError(f"unsafe path in archive: {m.name}")
            t.extractall(dest)
    else:
        raise ValueError("unsupported archive")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: inspect_archive.py <archive> [extract [dest]]", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "list"
    try:
        if mode == "list":
            items = entries(path)
            for name, size in items:
                print(f"{size:>12}  {name}")
            print(f"# {len(items)} files, {sum(s for _, s in items)} bytes uncompressed")
        elif mode == "extract":
            dest = sys.argv[3] if len(sys.argv) > 3 else path + "_extracted"
            extract(path, dest)
            print(f"extracted to {dest}")
        else:
            print("mode must be list or extract", file=sys.stderr); sys.exit(2)
    except (ValueError, tarfile.TarError, zipfile.BadZipFile) as e:
        print(f"error: {e}", file=sys.stderr); sys.exit(1)
