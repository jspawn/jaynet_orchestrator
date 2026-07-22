"""Slash commands — fast paths that bypass the model entirely.

Like the quick-reply greeting path, these never touch a model: `/help` renders
tool docs from the registry, and `/<tool.name>` executes a single tool call in
a normal ToolContext (the confirmation gate stays intact — the caller passes a
`confirm(name, args) -> bool` hook). The web server routes messages starting
with '/' here before the agent loop.
"""

from __future__ import annotations

import json
import shlex

from runtime.tool_base import ToolContext, ToolResult

_META = """`/` commands (answered directly — no agent loop; only `/compact` and `/wgs`
call a model):
- `/help` — this overview
- `/help tools` — every tool, grouped by namespace
- `/help <tool>` — full card for one tool (arguments, gating)
- `/compact [focus]` — summarize this chat's older history into a continuity
  brief (one call on the local brain) and continue from it; the last exchanges
  stay verbatim
- `/imp list` — models you can impersonate (local presets + cloud aliases)
- `/imp <model> [budget=<usd>] [ctxguard=<tokens>]` — route the brain to that
  model (user-bound, all devices; cloud aliases need a `confirm` keyword).
  `/impstop` (or `/imp off`) switches back to the default brain
- `/wgs [topic]` — start a skill-authoring session: a normal run with the
  writing-great-skills playbook force-loaded
- `/<tool> [args]` — run one tool directly, e.g. `/model.list`, `/gpu.status`,
  `/fs.read path=notes.md`. Args as `key=value` pairs or a JSON object;
  a single bare value maps to the tool's one required argument."""


def help_overview(registry) -> str:
    ns: dict[str, int] = {}
    for t in registry.all():
        root = t.name.split(".", 1)[0]
        ns[root] = ns.get(root, 0) + 1
    groups = ", ".join(f"{k} ({ns[k]})" for k in sorted(ns))
    return (_META + "\n\n**Tool namespaces:** " + groups +
            "\n\nTry `/help model.list` — or just run `/model.list`.")


def help_tools(registry) -> str:
    groups: dict[str, list[str]] = {}
    for t in registry.all():
        groups.setdefault(t.name.split(".", 1)[0], []).append(t.name)
    lines = ["**All tools by namespace** (`/help <name>` for details):"]
    for k in sorted(groups):
        lines.append(f"- **{k}** — " + ", ".join(f"`{n}`" for n in sorted(groups[k])))
    return "\n".join(lines)


def help_tool(tool) -> str:
    flags = []
    if tool.private:
        flags.append("private")
    if tool.requires_confirmation:
        flags.append("confirmation-gated")
    if tool.poll_safe:
        flags.append("poll-safe")
    lines = [f"**`{tool.name}`**" + (f"  ({', '.join(flags)})" if flags else "")]
    lines.append(tool.description)
    params = tool.parameters or {}
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    if props:
        lines.append("\n**Arguments:**")
        for name, spec in props.items():
            typ = spec.get("type", "any")
            req = " *(required)*" if name in required else ""
            default = (f" (default `{spec['default']!r}`)"
                       if "default" in spec and name not in required else "")
            desc = (spec.get("description") or "").split("\n")[0]
            lines.append(f"- `{name}`: {typ}{req}{default} — {desc}")
    else:
        lines.append("\nNo arguments.")
    return "\n".join(lines)


def parse_tool_args(tool, rest: str) -> dict:
    """Parse the text after `/<tool>` into an args dict: a JSON object, or
    key=value tokens (values JSON-coerced), or one bare value for a tool with
    exactly one required argument."""
    rest = (rest or "").strip()
    if not rest:
        return {}
    if rest.startswith("{"):
        obj = json.loads(rest)
        if not isinstance(obj, dict):
            raise ValueError("JSON args must be an object")
        return obj
    tokens = shlex.split(rest)
    if all("=" in t for t in tokens):
        out = {}
        for t in tokens:
            k, _, v = t.partition("=")
            try:
                out[k] = json.loads(v)
            except ValueError:
                out[k] = v
        return out
    required = list((tool.parameters or {}).get("required") or [])
    if len(tokens) == 1 and len(required) == 1:
        return {required[0]: tokens[0]}
    raise ValueError(
        f"couldn't parse args {rest!r} — use key=value pairs "
        f"(e.g. {' '.join(f'{r}=…' for r in required) or 'key=value'}) or a JSON object")


def _coerce_args(tool, args: dict) -> dict:
    """Best-effort coercion of string values to the schema's declared types."""
    props = (tool.parameters or {}).get("properties") or {}
    out = dict(args)
    for k, spec in props.items():
        if k not in out or not isinstance(out[k], str):
            continue
        v, typ = out[k], spec.get("type")
        try:
            if typ == "integer":
                out[k] = int(v)
            elif typ == "number":
                out[k] = float(v)
            elif typ == "boolean":
                out[k] = v.strip().lower() in ("1", "true", "yes", "on")
            elif typ in ("object", "array"):
                out[k] = json.loads(v)
        except (ValueError, TypeError):
            pass          # leave as-is; the tool validates
    return out


def _format_result(res: ToolResult, cap: int = 6000) -> str:
    if res.status != "ok":
        return f"**error** — {res.error}"
    body = json.dumps(res.result, indent=2, ensure_ascii=False, default=str)
    if len(body) > cap:
        body = body[:cap] + "\n… (truncated)"
    return f"```json\n{body}\n```"


async def run_slash(command: str, registry, ctx: ToolContext, confirm=None) -> str:
    """Execute one slash command line, returning the markdown to render."""
    head, _, rest = command.strip().partition(" ")
    name = head[1:]
    if name == "help":
        topic = rest.strip()
        if not topic:
            return help_overview(registry)
        if topic == "tools":
            return help_tools(registry)
        tool = registry.get(topic)
        return (help_tool(tool) if tool else
                f"no tool named `{topic}` — try `/help tools` for the full list.")
    tool = registry.get(name)
    if tool is None:
        return (f"unknown command `{head}`. `/help` lists the commands; "
                "`/help tools` lists every runnable tool.")
    try:
        args = _coerce_args(tool, parse_tool_args(tool, rest))
    except (ValueError, json.JSONDecodeError) as e:
        return f"**error** — {e}"
    if tool.needs_confirmation(args, ctx):
        approved = await confirm(name, args) if confirm else False
        if not approved:
            return f"declined — `{name}` was not run."
    try:
        result = await tool.execute(args, ctx)
    except Exception as e:
        return f"**error** — {type(e).__name__}: {e}"
    return _format_result(result)
