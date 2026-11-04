"""Deterministic auto-loading for format, long-document and diagnostic skills.

Unlike shape-tagged procedures, these triggers come from trusted attachment
metadata, input size, or an observed failure streak. Loading instructions is
the only automatic action; the model still chooses and runs the actual tools.
"""
from __future__ import annotations

import re

from runtime import paths
from runtime.skills import load_skill

_ATTACHMENT_RE = re.compile(
    r"^- (?P<name>.+?) \((?P<kind>text|binary|image), (?P<size>\d+) B\) saved at .+$",
    re.M)
_EXTENSIONS = {
    "pdf": (".pdf",),
    "docx": (".docx",),
    "xlsx": (".xlsx",),
    "pptx": (".pptx",),
    "archives": (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"),
    "audio": (".wav", ".mp3", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac"),
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
              ".tif", ".tiff"),
}
_DIAGNOSIS_RE = re.compile(
    r"\b(?:diagnos(?:e|is)|debug this|performance regression|intermittent failure|flaky test)\b",
    re.I)
_FILE_REF_RE = re.compile(r"<files\[['\"]([^'\"]+)['\"]\]>")


def initial_skill_choices(user_message: str, *, source_message: str = "",
                          document_chars: int = 12000) -> list[tuple[str, str]]:
    """Return ordered (skill, trigger) pairs; duplicate skills load only once."""
    if not isinstance(user_message, str) or not user_message:
        return []
    choices: list[tuple[str, str]] = []
    seen: set[str] = set()
    long_document = False
    matches = list(_ATTACHMENT_RE.finditer(user_message))
    for index, match in enumerate(matches):
        name = match.group("name").lower()
        for skill, extensions in _EXTENSIONS.items():
            if name.endswith(extensions) and skill not in seen:
                choices.append((skill, "attachment"))
                seen.add(skill)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(user_message)
        block = user_message[match.end():end]
        if match.group("kind") == "text" and (
                int(match.group("size")) > document_chars
                or "…[truncated]" in block):
            long_document = True
    probe = source_message if source_message else user_message
    if len(probe) > document_chars:
        long_document = True
    if long_document and "long-document" not in seen:
        choices.append(("long-document", "document_size"))
        seen.add("long-document")
    if _DIAGNOSIS_RE.search(probe) and "diagnosing-bugs" not in seen:
        choices.append(("diagnosing-bugs", "explicit_diagnosis"))
    return choices


def autoload_payload(runtime, name: str, *, allowed, disabled_skills=(),
                     skill_additions=None) -> dict | None:
    """Load one skill under the same availability policy as `skill.load`."""
    if allowed is not None and "skill.load" not in allowed:
        return None
    if runtime.registry.get("skill.load") is None or name in (disabled_skills or ()):
        return None
    skills_dir = (runtime.config.get("skills") or {}).get(
        "dir", str(paths.SKILLS_DIR))
    payload = load_skill(skills_dir, name, custom_dir=paths.CUSTOM_SKILLS_DIR)
    if payload is None:
        return None
    addition = (skill_additions or {}).get(name)
    if addition:
        payload = {**payload, "instructions": (
            str(payload["instructions"]).rstrip()
            + "\n\n— Experiment addition —\n" + str(addition).strip())}
    return payload


def render_autoload(payload: dict, trigger: str) -> str:
    """Render instructions with usable absolute bundled-script paths."""
    files = payload.get("files") or {}
    body = str(payload.get("instructions") or "")
    body = _FILE_REF_RE.sub(lambda m: repr(files.get(m.group(1), m.group(0))), body)
    if files:
        body += "\n\nBundled files (absolute paths):\n" + "\n".join(
            f"- {name}: {path}" for name, path in sorted(files.items()))
    return (f"Skill auto-loaded ({payload['name']}; trigger: {trigger}) — "
            f"follow its instructions:\n\n{body}")
