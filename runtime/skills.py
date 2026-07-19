"""Runtime-loadable skills — packaged playbooks the model pulls in on demand.

A skill is a directory under the skills root containing a `SKILL.md`:

    skills/<name>/SKILL.md      # frontmatter (name, description) + instructions
    skills/<name>/*             # optional bundled resources / scripts

Two-tier (progressive) disclosure, like Anthropic's own Skills:
  - The lightweight catalog (name + description) is injected into the system
    prompt so the model knows what exists and WHEN to reach for it — cheap, always
    present.
  - The full body (and bundled file paths) is returned only when the model calls
    `skill.load(<name>)`, so long instructions don't sit in every context.

This module is the single source of truth for both: the loop renders the catalog,
the skill tools load bodies. No third-party deps beyond pyyaml (already used for
config).
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a `---`-delimited YAML frontmatter block from the markdown body."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                meta = {}
            if isinstance(meta, dict):
                return meta, parts[2].lstrip("\n")
    return {}, text


def discover_skills(skills_dir: str | Path) -> dict[str, dict]:
    """Scan the skills root, returning {name: skill_dict} sorted by name.

    skill_dict = {name, description, dir, skill_md, body, resources[]}.
    Resilient: a missing root or unreadable SKILL.md yields no/skip entries.
    """
    root = Path(skills_dir)
    out: dict[str, dict] = {}
    if not root.is_dir():
        return out
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        md = sub / "SKILL.md"
        if not md.is_file():
            continue
        try:
            meta, body = _parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        name = str(meta.get("name") or sub.name)
        resources = sorted(
            str(p.relative_to(sub)) for p in sub.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )
        out[name] = {
            "name": name,
            "description": str(meta.get("description") or "").strip(),
            "dir": str(sub),
            "skill_md": str(md),
            "body": body,
            "resources": resources,
        }
    return dict(sorted(out.items()))


# Per-process discovery cache for the skill TOOLS. skill.load / skill.list used
# to rescan the whole tree (directory walk + file reads) on every call; the tree
# only changes on deploy, which restarts the process anyway. Keyed by dir path.
_DISCOVERY_CACHE: dict[str, dict[str, dict]] = {}


def discover_skills_cached(skills_dir: str | Path) -> dict[str, dict]:
    """discover_skills, memoized per process + dir. The loop's init-time catalog
    scan stays on the uncached variant; tests that mutate a skills dir should
    call skills_cache_clear()."""
    key = str(skills_dir)
    skills = _DISCOVERY_CACHE.get(key)
    if skills is None:
        skills = discover_skills(skills_dir)
        _DISCOVERY_CACHE[key] = skills
    return skills


def skills_cache_clear() -> None:
    _DISCOVERY_CACHE.clear()


def render_catalog(skills: dict[str, dict]) -> str:
    """The always-present catalog injected into the system prompt. Names +
    one-line descriptions only — never the bodies."""
    if not skills:
        return ""
    lines = [
        "## Available skills",
        "You have skills — packaged playbooks for specific tasks, each with a note on "
        "when to use it. When a task matches one, call `skill.load(\"<name>\")` to get "
        "its full instructions and the paths of any bundled files, then follow them "
        "using your normal tools. Load a skill only when its trigger applies; don't "
        "preload, and ignore the rest.",
        "",
    ]
    for s in skills.values():
        lines.append(f"- **{s['name']}** — {s['description']}")
    return "\n".join(lines)


def load_skill(skills_dir: str | Path, name: str) -> dict | None:
    """Full skill payload for `skill.load`: body + absolute paths of bundled
    files (so the model can run scripts / read references). None if not found."""
    skills = discover_skills_cached(skills_dir)
    s = skills.get(name)
    if not s:
        return None
    files = {r: str(Path(s["dir"]) / r) for r in s["resources"]}
    return {
        "name": s["name"],
        "description": s["description"],
        "instructions": s["body"],
        "dir": s["dir"],
        "files": files,
    }
