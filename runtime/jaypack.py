"""jaypack — export/import of Studio artifacts as shareable `.jaypack` bundles.

A .jaypack is a small zip:

    jaypack.yaml      # {kind, name, version, description, author, files: [...]}
    payload/...       # the artifact's natural files, relative shape preserved:
                      #   skill     payload/<name>/SKILL.md (+resources)
                      #   chain     payload/<name>.yaml
                      #   connector payload/<name>.yaml
                      #   tool      payload/<ns>/<verb>.py
                      #   eval      payload/<id>.yaml

Skills and chains may be exported from EITHER the builtin or the custom layer
(custom first — that's how sharing a tweaked builtin starts); eval cases too
(builtin seeds live in <repo>/evals); tools and
connectors only exist in the custom area. Installs always land in the custom
area (ORCH_DATA/custom), which is created lazily here.

Guards: kind whitelist, name regex (shared with chains), 5 MB compressed cap,
zip-slip rejection (every member must stay under the target dir), the expected
primary file must be present, and installs refuse to clobber an existing
artifact unless overwrite=True.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from tools.chain.engine import _NAME_OK

_MAX_BYTES = 5 * 1024 * 1024     # compressed cap
KINDS = ("skill", "chain", "connector", "tool", "eval")
_MANIFEST = "jaypack.yaml"
_PAYLOAD = "payload/"


class JaypackError(Exception):
    """User-actionable pack failure (bad kind/name, malformed zip, guards)."""


@dataclass
class Roots:
    """Filesystem roots a pack is built against / installed into. Tests point
    these at tmp_path; production uses default_roots() (runtime.paths)."""
    skills_builtin: Path
    skills_custom: Path
    chains_builtin: Path
    chains_custom: Path
    conn_custom: Path
    tools_custom: Path
    evals_builtin: Path
    evals_custom: Path


def default_roots() -> Roots:
    from runtime import paths
    from tools.chain.engine import chains_dir
    return Roots(
        skills_builtin=paths.SKILLS_DIR,
        skills_custom=paths.CUSTOM_SKILLS_DIR,
        chains_builtin=chains_dir({}),
        chains_custom=paths.CUSTOM_CHAINS_DIR,
        conn_custom=paths.CUSTOM_CONN_DIR,
        tools_custom=paths.CUSTOM_TOOLS_DIR,
        evals_builtin=paths.HOME / "evals",
        evals_custom=paths.CUSTOM_EVALS_DIR,
    )


def _check(kind: str, name: str) -> None:
    if kind not in KINDS:
        raise JaypackError(f"invalid kind '{kind}' (one of {', '.join(KINDS)})")
    if not _NAME_OK.match(name or ""):
        raise JaypackError(f"invalid {kind} name '{name}' "
                           f"(letters, digits, dash, underscore)")


def _payload_files(kind: str, name: str, roots: Roots) -> dict[str, bytes]:
    """Collect {payload-relative path: content} for an export, custom first."""
    if kind == "skill":
        src = roots.skills_custom / name
        if not src.is_dir():
            src = roots.skills_builtin / name
        if not src.is_dir():
            raise JaypackError(f"no skill '{name}' in {roots.skills_custom} "
                               f"or {roots.skills_builtin}")
        return {f"{name}/{p.relative_to(src).as_posix()}": p.read_bytes()
                for p in sorted(src.rglob("*")) if p.is_file()}
    if kind == "chain":
        for base in (roots.chains_custom, roots.chains_builtin):
            f = base / f"{name}.yaml"
            if f.is_file():
                return {f"{name}.yaml": f.read_bytes()}
        raise JaypackError(f"no chain '{name}' in {roots.chains_custom} "
                           f"or {roots.chains_builtin}")
    if kind == "connector":
        f = roots.conn_custom / f"{name}.yaml"
        if not f.is_file():
            raise JaypackError(f"no connector '{name}' in {roots.conn_custom}")
        return {f"{name}.yaml": f.read_bytes()}
    if kind == "eval":
        for base in (roots.evals_custom, roots.evals_builtin):
            f = base / f"{name}.yaml"
            if f.is_file():
                return {f"{name}.yaml": f.read_bytes()}
        raise JaypackError(f"no eval case '{name}' in {roots.evals_custom} "
                           f"or {roots.evals_builtin}")
    # tool: one .py, relative shape <ns>/<verb>.py preserved
    matches = sorted(roots.tools_custom.rglob(f"{name}.py")) \
        if roots.tools_custom.is_dir() else []
    if len(matches) != 1:
        raise JaypackError(f"expected exactly one custom tool '{name}.py' under "
                           f"{roots.tools_custom}, found {len(matches)}")
    f = matches[0]
    return {f.relative_to(roots.tools_custom).as_posix(): f.read_bytes()}


def build_pack(kind: str, name: str, roots: Roots | None = None, *,
               version: str = "1.0", description: str = "",
               author: str = "") -> bytes:
    """Zip the named artifact into .jaypack bytes."""
    roots = roots or default_roots()
    _check(kind, name)
    files = _payload_files(kind, name, roots)
    manifest = {"kind": kind, "name": name, "version": str(version),
                "description": str(description), "author": str(author),
                "files": sorted(files)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(_MANIFEST, yaml.safe_dump(manifest))
        for rel, content in files.items():
            z.writestr(_PAYLOAD + rel, content)
    return buf.getvalue()


def _load(data: bytes) -> tuple[zipfile.ZipFile, dict]:
    """Open a pack and apply every guard that doesn't touch the filesystem."""
    if len(data) > _MAX_BYTES:
        raise JaypackError(f"pack is {len(data)} bytes (max {_MAX_BYTES})")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise JaypackError(f"not a zip file: {e}") from e
    names = z.namelist()
    if _MANIFEST not in names:
        raise JaypackError(f"pack has no {_MANIFEST}")
    try:
        manifest = yaml.safe_load(z.read(_MANIFEST).decode("utf-8"))
    except yaml.YAMLError as e:
        raise JaypackError(f"bad {_MANIFEST}: {e}") from e
    if not isinstance(manifest, dict):
        raise JaypackError(f"{_MANIFEST} must be a mapping")
    _check(str(manifest.get("kind") or ""), str(manifest.get("name") or ""))
    members = [n for n in names if n.startswith(_PAYLOAD) and not n.endswith("/")]
    if any(n != _MANIFEST and not n.startswith(_PAYLOAD) for n in names):
        raise JaypackError("pack contains files outside payload/")
    for m in members:
        rel = m[len(_PAYLOAD):]
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts:
            raise JaypackError(f"unsafe path in pack: {m}")
    kind, name = manifest["kind"], manifest["name"]
    if kind == "skill":
        ok = f"{name}/SKILL.md" in {m[len(_PAYLOAD):] for m in members}
    elif kind in ("chain", "connector", "eval"):
        ok = f"{name}.yaml" in {m[len(_PAYLOAD):] for m in members}
    else:  # tool: exactly one .py under payload/
        ok = sum(1 for m in members if m.endswith(".py")) == 1
    if not members or not ok:
        raise JaypackError(f"pack payload is missing the expected {kind} file")
    return z, manifest


def inspect_pack(data: bytes) -> dict:
    """Validate a pack and return its manifest {kind, name, version, ...}."""
    z, manifest = _load(data)
    z.close()
    return manifest


def _target_base(kind: str, roots: Roots) -> Path:
    return {"skill": roots.skills_custom, "chain": roots.chains_custom,
            "connector": roots.conn_custom, "tool": roots.tools_custom,
            "eval": roots.evals_custom}[kind]


def install_pack(data: bytes, overwrite: bool = False,
                 roots: Roots | None = None) -> dict:
    """Install a pack into the custom area. Raises FileExistsError when the
    target already exists and overwrite is False."""
    roots = roots or default_roots()
    z, manifest = _load(data)
    kind, name = manifest["kind"], manifest["name"]
    base = _target_base(kind, roots).resolve()
    members = [n for n in z.namelist()
               if n.startswith(_PAYLOAD) and not n.endswith("/")]
    targets: list[tuple[str, Path]] = []
    for m in members:
        rel = m[len(_PAYLOAD):]
        dest = (base / rel).resolve()
        if dest != base and base not in dest.parents:   # zip-slip backstop
            raise JaypackError(f"unsafe path in pack: {m}")
        targets.append((m, dest))
    if not overwrite:
        existing = [str(d) for _, d in targets if d.exists()]
        if existing:
            z.close()
            raise FileExistsError(
                f"{kind} '{name}' already exists at {existing[0]} "
                f"— pass overwrite=True to replace it")
    for m, dest in targets:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(z.read(m))
    z.close()
    if kind == "skill":
        installed_path = base / name
    elif kind in ("chain", "connector", "eval"):
        installed_path = base / f"{name}.yaml"
    else:
        installed_path = targets[0][1]
    return {"installed": name, "path": str(installed_path)}
