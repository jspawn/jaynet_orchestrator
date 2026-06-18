"""Skills — `skill.load` / `skill.list`.

Skills are packaged playbooks on disk (see runtime/skills.py). Their catalog
(name + when-to-use) is already injected into the system prompt; these tools let
the model pull a skill's full instructions — and the paths of any bundled files —
into the conversation on demand, then act on them with its normal tools.

Not private (skill bodies are local instructions, not user data) and not
confirmation-gated (loading instructions is harmless; any action the skill then
suggests goes through that action's own tool, with its own gating).
"""

from __future__ import annotations

from runtime.skills import discover_skills, load_skill
from runtime.tool_base import Tool, ToolContext, ToolResult


def _skills_dir(ctx: ToolContext) -> str:
    sk = (ctx.config.get("skills", {}) or {})
    return sk.get("dir", "/srv/orchestrator/skills")


class SkillLoad(Tool):
    name = "skill.load"
    description = (
        "Load a skill's full instructions (and the absolute paths of any bundled "
        "files, e.g. helper scripts) by name. Call this when a task matches one of "
        "the skills listed under 'Available skills'. Then follow the returned "
        "instructions using your normal tools."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The skill name to load."},
        },
        "required": ["name"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        name = (args.get("name") or "").strip()
        payload = load_skill(_skills_dir(ctx), name)
        if payload is None:
            available = ", ".join(discover_skills(_skills_dir(ctx)).keys()) or "(none)"
            return ToolResult(status="error", result=None,
                              error=f"no such skill: {name!r}. Available: {available}")
        return ToolResult(status="ok", result=payload)


class SkillList(Tool):
    name = "skill.list"
    description = ("List the available skills with their descriptions. (The same "
                   "catalog is already in your system prompt; use this only if you "
                   "need to re-check what's available.)")
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        skills = discover_skills(_skills_dir(ctx))
        return ToolResult(status="ok", result={"skills": [
            {"name": s["name"], "description": s["description"],
             "resources": s["resources"]}
            for s in skills.values()
        ]})
