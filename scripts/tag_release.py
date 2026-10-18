#!/usr/bin/env python3
"""Validate committed release metadata before creating a lightweight tag.

Usage:
  scripts/tag_release.py vX.Y.Z           # validate HEAD, then create the tag
  scripts/tag_release.py vX.Y.Z --check   # validate only; never mutate git

The checks read HEAD through git, not the possibly-dirty working tree.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, text=True,
                          capture_output=True, check=False)


def _head_text(path: str) -> str | None:
    proc = _git("show", f"HEAD:{path}")
    return proc.stdout if proc.returncode == 0 else None


def _metadata_errors(tag: str) -> list[str]:
    version = tag.removeprefix("v")
    runtime_text = _head_text("runtime/__init__.py")
    changelog = _head_text("CHANGELOG.md")
    release = _head_text(f"docs/releases/{tag}.md")
    errors: list[str] = []

    match = re.search(r"(?m)^__version__\s*=\s*['\"]([^'\"]+)['\"]",
                      runtime_text or "")
    runtime_version = match.group(1) if match else None
    if runtime_version != version:
        errors.append(f"HEAD runtime.__version__ is {runtime_version!r}, "
                      f"expected {version!r}")

    match = re.search(r"(?m)^## (\d+\.\d+\.\d+)", changelog or "")
    changelog_version = match.group(1) if match else None
    if changelog_version != version:
        errors.append(f"HEAD CHANGELOG top entry is {changelog_version!r}, "
                      f"expected {version!r}")

    if release is None:
        errors.append(f"HEAD has no docs/releases/{tag}.md")
    else:
        heading = next((line for line in release.splitlines()
                        if line.startswith("# ")), "")
        if not re.match(rf"^# (?:JayNet )?v{re.escape(version)}(?:\s|$)",
                        heading):
            errors.append(f"release heading {heading!r} does not name {tag}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag in vX.Y.Z form")
    parser.add_argument("--check", action="store_true",
                        help="validate HEAD without creating a tag")
    args = parser.parse_args()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.tag):
        parser.error("tag must use vX.Y.Z semver form")

    errors = _metadata_errors(args.tag)
    if errors:
        for error in errors:
            print(f"tag-release: {error}", file=sys.stderr)
        return 1
    print(f"tag-release: HEAD metadata matches {args.tag}")
    if args.check:
        return 0

    existing = _git("rev-parse", "--verify", "--quiet",
                    f"refs/tags/{args.tag}")
    if existing.returncode == 0:
        print(f"tag-release: {args.tag} already exists", file=sys.stderr)
        return 1
    if existing.returncode != 1:
        print(existing.stderr.strip(), file=sys.stderr)
        return 1

    head = _git("rev-parse", "HEAD")
    if head.returncode != 0:
        print(head.stderr.strip(), file=sys.stderr)
        return 1
    commit = head.stdout.strip()
    created = _git("tag", args.tag, commit)
    if created.returncode != 0:
        print(created.stderr.strip(), file=sys.stderr)
        return 1
    print(f"tag-release: created {args.tag} at {commit[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
