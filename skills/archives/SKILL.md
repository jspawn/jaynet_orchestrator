---
name: archives
description: Inspect or extract .zip, .tar, .tar.gz/.tgz, and .tar.bz2 archives. Load when an archive is uploaded or referenced.
---
# Inspecting and extracting archives

The bundled `inspect_archive.py` is standard-library only (`zipfile` + `tarfile`)
and has a path-traversal guard on extraction.

## List contents first

    job.start(name="list-archive",
              command="python <files['inspect_archive.py']> <path-to-archive>")

Prints `size  name` per file plus a total. Use this to decide what matters before
extracting anything.

## Extract when needed

    job.start(name="extract-archive",
              command="python <files['inspect_archive.py']> <archive> extract <dest_dir>")

Pick a `dest_dir` under the orchestrator's data/work area. After extraction, use
`fs.list` / `fs.read` (or load another skill, e.g. `xlsx`/`pdf`) on the files.

## Notes

- Be selective: a large archive can contain thousands of files — list first,
  extract only what the task needs, and don't dump everything into context.
- Supported: `.zip`, `.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`. For `.7z` or `.rar`
  you'd need `py7zr` / `unrar` in a venv — tell the user if that's the case.
- The script refuses entries that would write outside `dest_dir` (zip-slip
  protection); if it errors on an unsafe path, report it rather than overriding.
