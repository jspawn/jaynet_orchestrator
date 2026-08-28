"""jaypack — export/import of Studio artifacts as shareable `.jaypack` bundles.

A .jaypack is a small zip:

    jaypack.yaml      # {kind, name, version, description, author, files: [...]}
    payload/...       # the artifact's natural files, relative shape preserved:
                      #   skill     payload/<name>/SKILL.md (+resources)
                      #   chain     payload/<name>.yaml
                      #   connector payload/<name>.yaml
                      #   tool      payload/<ns>/<verb>.py
                      #   eval      payload/<id>.yaml
                      #   plugin    payload/<name>/plugin.yaml (+tools/, ui/, …)

Skills and chains may be exported from EITHER the builtin or the custom layer
(custom first — that's how sharing a tweaked builtin starts); eval cases too
(builtin seeds live in <repo>/evals); tools and
connectors only exist in the custom area. Plugins export from the installed
layer first, then builtin. Installs always land in the custom
area (ORCH_DATA/custom) — plugins in ORCH_DATA/plugins — created lazily here.
A plugin pack carries executable Python: the trust model is the same as the
"tool" kind and manual drop-in — only install code you audited.

Guards: kind whitelist, name regex (shared with chains), 5 MB compressed and
20 MB uncompressed size caps, zip-slip rejection (every member must stay under
the target dir), the expected primary file must be present, and installs
refuse to clobber an existing artifact unless overwrite=True. A plugin pack's
inner plugin.yaml must additionally parse as a mapping and (when it names the
plugin) match the pack name — otherwise install fails at upload time instead
of surfacing as "unavailable" after a restart.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from tools.chain.engine import _NAME_OK

_MAX_BYTES = 5 * 1024 * 1024          # compressed cap
_MAX_UNCOMPRESSED = 20 * 1024 * 1024  # uncompressed payload cap (zip-bomb guard)
KINDS = ("skill", "chain", "connector", "tool", "eval", "plugin")
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
    plugins_builtin: Path
    plugins_installed: Path


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
        plugins_builtin=paths.PLUGINS_BUILTIN_DIR,
        plugins_installed=paths.PLUGINS_DIR,
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
        src = roots.conn_custom / name
        if src.is_dir():
            # Package shape (<id>/connector.yaml + README.md …): the whole
            # dir — state never lives there, so a pack is shareable as-is.
            return {f"{name}/{p.relative_to(src).as_posix()}": p.read_bytes()
                    for p in sorted(src.rglob("*")) if p.is_file()
                    and "__pycache__" not in p.parts}
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
    if kind == "plugin":
        src = roots.plugins_installed / name
        if not src.is_dir():
            src = roots.plugins_builtin / name
        if not src.is_dir():
            raise JaypackError(f"no plugin '{name}' in {roots.plugins_installed} "
                               f"or {roots.plugins_builtin}")
        return {f"{name}/{p.relative_to(src).as_posix()}": p.read_bytes()
                for p in sorted(src.rglob("*")) if p.is_file()
                and "__pycache__" not in p.parts}
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
    # Zip-bomb guard: the compressed cap above says nothing about expansion and
    # members are read fully into memory on install, so cap the declared
    # uncompressed payload total too.
    if sum(z.getinfo(m).file_size for m in members) > _MAX_UNCOMPRESSED:
        raise JaypackError(f"pack payload expands past {_MAX_UNCOMPRESSED} "
                           f"bytes uncompressed")
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
    elif kind == "plugin":
        ok = f"{name}/plugin.yaml" in {m[len(_PAYLOAD):] for m in members}
    elif kind == "connector":
        rels = {m[len(_PAYLOAD):] for m in members}
        ok = f"{name}.yaml" in rels or f"{name}/connector.yaml" in rels
    elif kind in ("chain", "eval"):
        ok = f"{name}.yaml" in {m[len(_PAYLOAD):] for m in members}
    else:  # tool: exactly one .py under payload/
        ok = sum(1 for m in members if m.endswith(".py")) == 1
    if not members or not ok:
        raise JaypackError(f"pack payload is missing the expected {kind} file")
    if kind == "plugin":
        # A malformed inner plugin.yaml would install fine and only surface
        # as "unavailable" after a restart — catch it at install time. Boot
        # treats name as optional (falls back to the dir name), but when it
        # IS present it must match the pack name, or the plugin would scan
        # under a different name than it was installed as.
        try:
            inner = yaml.safe_load(z.read(f"{_PAYLOAD}{name}/plugin.yaml")) or {}
        except yaml.YAMLError as e:
            raise JaypackError(f"plugin.yaml in pack is not valid YAML: {e}")
        if not isinstance(inner, dict):
            raise JaypackError("plugin.yaml in pack is not a mapping")
        if inner.get("name") is not None and str(inner["name"]) != name:
            raise JaypackError(
                f"plugin.yaml name '{inner['name']}' does not match "
                f"pack name '{name}'")
    return z, manifest


def inspect_pack(data: bytes) -> dict:
    """Validate a pack and return its manifest {kind, name, version, ...}."""
    z, manifest = _load(data)
    z.close()
    return manifest


def _target_base(kind: str, roots: Roots) -> Path:
    return {"skill": roots.skills_custom, "chain": roots.chains_custom,
            "connector": roots.conn_custom, "tool": roots.tools_custom,
            "eval": roots.evals_custom,
            "plugin": roots.plugins_installed}[kind]


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
    if kind in ("skill", "plugin"):
        installed_path = base / name
    elif kind == "connector" and (base / name).is_dir():
        installed_path = base / name                # package shape
    elif kind in ("chain", "connector", "eval"):
        installed_path = base / f"{name}.yaml"
    else:
        installed_path = targets[0][1]
    return {"installed": name, "path": str(installed_path)}
