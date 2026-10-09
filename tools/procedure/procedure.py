"""procedure.* — model-authored, persisted, re-runnable mini-programs.

The reasonlet/metacache idea, JayNet-shaped: when a task boils down to a
deterministic procedure (a formula, a conversion, a fixed multi-step check),
the model writes it ONCE as a small script, saved into the run's workspace
under procedures/. Later runs re-execute it with new inputs via
procedure.run — zero model tokens, byte-identical logic, inspectable and
editable as plain project files.

Authorship vs reuse: the coding specialist (or the brain on small installs)
authors via procedure.save; the brain's steady-state move is procedure.run —
a dumb, cheap tool call. Re-running never asks the model to re-derive the
logic, so drift between identical questions disappears.

Execution rides the code.check engine (same sandbox, network off, 120s cap):
a failing procedure is a normal ok result with a non-zero exit, exactly like
a failing test. Files are plain text — rename/edit/delete them with the fs.*
tools; these tools are just the disciplined front door.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import time
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.code.check import CodeCheck

log = logging.getLogger(__name__)

DIR_NAME = "procedures"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")
_EXTS = {"python": ".py", "bash": ".sh"}


def _dir(ctx: ToolContext) -> Path | None:
    """The procedures dir for this run (created lazily on save)."""
    root = getattr(ctx, "work_root", None)
    if not root:
        return None
    return Path(root) / DIR_NAME


def _sidecar_path(base: Path, name: str) -> Path:
    return base / f"{name}.json"


def _script_path(base: Path, name: str, language: str) -> Path:
    return base / f"{name}{_EXTS[language]}"


def _read_meta(base: Path, name: str) -> dict | None:
    try:
        return json.loads(_sidecar_path(base, name).read_text(encoding="utf-8"))
    except Exception:
        return None


class ProcedureSave(Tool):
    name = "procedure.save"
    description = (
        "Save a reusable procedure: a short python or bash script with a "
        "name, stored in the workspace's procedures/ folder. Reach for this "
        "when the task just solved is deterministic and likely to recur "
        "(a formula, a conversion, a fixed check pipeline) — later you (or a "
        "future run) call procedure.run with new inputs instead of "
        "re-deriving the logic. Write the script to read its inputs from "
        "the ARGS dict (python) or the PROC_ARGS env var as JSON (bash), "
        "and print() the result. Saving under an existing name REPLACES it."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Slug: lowercase letters, digits, dashes "
                               "(e.g. 'compound-interest').",
            },
            "description": {
                "type": "string",
                "description": "One line: what it computes and what inputs "
                               "it takes (shown by procedure.list).",
            },
            "code": {
                "type": "string",
                "description": "The script. python (default): inputs in the "
                               "pre-defined ARGS dict, print() the result. "
                               "bash: inputs as JSON in $PROC_ARGS.",
            },
            "language": {
                "type": "string", "enum": ["python", "bash"],
                "default": "python",
            },
        },
        "required": ["name", "description", "code"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        name = (args.get("name") or "").strip().lower()
        if not _NAME_RE.match(name):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"invalid name {name!r} — lowercase slug "
                                    "(a-z, 0-9, dashes, max 49 chars)")
        base = _dir(ctx)
        if base is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="procedures need a project workspace "
                                    "(no work_root in this run)")
        language = args.get("language") or "python"
        if language not in _EXTS:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"unknown language {language!r}")
        code = (args.get("code") or "").rstrip() + "\n"
        description = (args.get("description") or "").strip()
        try:
            base.mkdir(parents=True, exist_ok=True)
            _script_path(base, name, language).write_text(code, encoding="utf-8")
            old = _read_meta(base, name) or {}
            meta = {"name": name, "description": description,
                    "language": language,
                    "created": old.get("created") or time.time(),
                    "updated": time.time()}
            _sidecar_path(base, name).write_text(
                json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"could not save procedure: {e}")
        return ToolResult(status="ok", tool_name=self.name,
                          result={"saved": name, "language": language,
                                  "path": str(_script_path(base, name, language)),
                                  "replaced": bool(old),
                                  "run": f'procedure.run name="{name}"'})


class ProcedureList(Tool):
    name = "procedure.list"
    description = (
        "List the reusable procedures saved in this workspace (procedure.save) "
        "with their descriptions — check here before re-deriving logic that "
        "may already be scripted."
    )
    private = True
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        base = _dir(ctx)
        if base is None or not base.is_dir():
            return ToolResult(status="ok", tool_name=self.name,
                              result={"procedures": [], "count": 0})
        out = []
        for meta_file in sorted(base.glob("*.json")):
            meta = _read_meta(base, meta_file.stem)
            if meta:
                out.append({"name": meta["name"],
                            "description": meta.get("description", ""),
                            "language": meta.get("language", "python")})
        return ToolResult(status="ok", tool_name=self.name,
                          result={"procedures": out,
                                  "count": len(out)})


class ProcedureRun(Tool):
    name = "procedure.run"
    description = (
        "Run a saved procedure (procedure.list shows what's available) with "
        "new inputs — deterministic, no re-derivation. Inputs ride as the "
        "ARGS dict (python) or $PROC_ARGS JSON (bash); the script's printed "
        "output is the result. Runs on the code.check engine: sandboxed, "
        "network off, 120s cap; a non-zero exit is a normal result — read "
        "stdout/stderr. To change what a procedure does, edit its file in "
        "procedures/ with fs.edit, don't save a copy under a new name."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The procedure slug (procedure.list to browse).",
            },
            "args": {
                "type": "object",
                "description": "Inputs for this run, exposed to the script as "
                               "ARGS (python) / $PROC_ARGS (bash).",
            },
        },
        "required": ["name"],
    }

    def needs_confirmation(self, args: dict, ctx: ToolContext) -> bool:
        # Same engine, same policy as code.check.
        return CodeCheck().needs_confirmation({"command": "x"}, ctx)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        base = _dir(ctx)
        if base is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="procedures need a project workspace "
                                    "(no work_root in this run)")
        name = (args.get("name") or "").strip().lower()
        meta = _read_meta(base, name)
        if not meta:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"no procedure {name!r} in this workspace "
                                    "— procedure.list to browse, "
                                    "procedure.save to create one")
        script = _script_path(base, name, meta.get("language", "python"))
        try:
            code = script.read_text(encoding="utf-8")
        except OSError as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"procedure {name!r} unreadable: {e}")
        inputs = args.get("args") or {}
        if meta.get("language") == "bash":
            command = f"PROC_ARGS={shlex.quote(json.dumps(inputs))} bash {shlex.quote(str(script))}"
            language = "bash"
        else:
            # Double json.dumps: the inner produces the JSON text, the outer
            # makes it a safe Python string literal on any payload.
            command = ("import json as _json\n"
                       f"ARGS = _json.loads({json.dumps(json.dumps(inputs))})\n"
                       + code)
            language = "python"
        result = await CodeCheck().execute(
            {"command": command, "language": language}, ctx)
        result.tool_name = self.name
        if isinstance(result.result, dict):
            result.result["procedure"] = name
        return result
