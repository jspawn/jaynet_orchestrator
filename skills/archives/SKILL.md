---
name: archives
description: Inspect, extract, or create .zip / .tar / .tar.gz/.tgz / .tar.bz2 / .tar.xz archives. Load when an archive is uploaded or referenced, or when asked to bundle/package files into an archive.
---
# Inspecting, extracting and creating archives

Two first-class tools handle this, both confined to the allowed file roots and
hardened against path-traversal, symlinks and decompression bombs:

- **`archives.extract(archive, dest?)`** — safely unpack a `.zip` or any `.tar*`
  variant. `dest` defaults to a folder named after the archive, beside it.
  Returns a bounded manifest of what was written.
- **`archives.create(paths, output, format?)`** — bundle files/dirs into an
  archive. `format`: `tar.gz` (default), `tgz`, `tar`, `tar.bz2`, `tar.xz`, `zip`.
  Junk dirs (`.git`, `__pycache__`, `.venv`, `node_modules`) are skipped.

## Extract

    archives.extract(archive="<path>.tar.gz", dest="<dir under data/work>")

After extraction, use `fs.list` / `fs.read` (or load another skill, e.g.
`xlsx`/`pdf`) on the files. **Be selective:** a big archive can hold thousands of
files. If you only need a peek at the file list first, run the bundled
`inspect_archive.py` (stdlib-only) via a job:

    job.start(name="list-archive",
              command="python <files['inspect_archive.py']> <archive>")

which prints `size  name` per entry plus a total — decide what matters before
extracting.

## Create

    archives.create(paths=["<dir-or-file>", ...], output="<path>.tar.gz", format="tar.gz")

Write the `output` under the data/work area (or the active project's files dir).
Use `zip` when the recipient is on Windows; `tar.gz` otherwise.

## Notes

- Both tools require confirmation (they write to disk) and refuse anything outside
  the allowed roots — work under the data/work area or a project.
- Extraction refuses unsafe members (paths escaping the destination, symlinks,
  special files) and caps file count / total uncompressed size. If it errors on an
  unsafe entry, report it rather than trying to override.
- For `.7z` or `.rar` you'd need `py7zr` / `unrar` in a venv — tell the user.
