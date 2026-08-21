"""graph.* tools — agent access to the current project's graph (graphify).

Not kg.*: kg is the curated fact/relation store the user teaches across
chats; graph.* is the auto-built map of THIS project's files and symbols.

All private: the graph is derived from project files, so its content is
tainted by construction and must never leave the box.

The graph is queried through the graphify CLI as a subprocess (no library
import — the plugin's only hard dependency is the CLI being installed).
Output is graphify's NODE/EDGE text format, which is exactly what the model
consumes best; it's passed through with a size cap.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult


def _load_runner():
    """Import the plugin's runner.py by file path, ONCE per process. Plugin
    modules are loaded via spec_from_file_location (not as a package), and
    installed plugins live outside the repo — a plain
    `import plugins.graphify.runner` would fail there. Every exec_module
    creates a fresh module with fresh module-level state (the _jobs dict!),
    so all entry files MUST share one sys.modules entry under one name."""
    name = "graphify_plugin_runner"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parents[1] / "runner.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


runner = _load_runner()

_QUERY_TIMEOUT_S = 60
_MAX_OUTPUT_CHARS = 12000


def _projects_dir(ctx: ToolContext) -> Path:
    from runtime.paths import PROJECTS_DIR
    web = ctx.config.get("web") or {}
    return Path(web.get("projects_dir") or PROJECTS_DIR)


def _require_project(ctx: ToolContext) -> tuple[Path, str] | ToolResult:
    """(graph_root, pid) for this run's project, or an error ToolResult."""
    pid = getattr(ctx, "project_id", None)
    if not pid:
        return ToolResult(status="error", result=None, error=(
            "no project bound to this run — project graphs exist per project. "
            "Ask the user to promote the chat to a project first."))
    root = runner.graph_root(_projects_dir(ctx), ctx.owner, pid)
    if not (root / "graph.json").is_file():
        return ToolResult(status="error", result=None, error=(
            "this project has no project graph yet — run graph.build first "
            "(takes a while for large projects)."))
    return root, pid


async def _query_cli(graph_json: Path, *cli_args: str) -> ToolResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "graphify", *cli_args,
            "--graph", str(graph_json),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_QUERY_TIMEOUT_S)
    except TimeoutError:
        return ToolResult(status="error", result=None, error="graph query timed out")
    except OSError as e:
        return ToolResult(status="error", result=None, error=f"graphify CLI failed: {e}")
    text = out.decode("utf-8", "replace").strip()
    if len(text) > _MAX_OUTPUT_CHARS:
        text = text[:_MAX_OUTPUT_CHARS] + "\n…[truncated]"
    if proc.returncode != 0 and not text:
        return ToolResult(status="error", result=None, error=f"graphify exited {proc.returncode}")
    return ToolResult(status="ok", result={"output": text or "(no matching nodes)"})


class GraphBuild(Tool):
    name = "graph.build"
    description = ("Build or refresh the project graph of the current project "
                   "(code via local AST, docs via the configured local model). "
                   "Runs in the background; poll graph.status until state is "
                   "'ready'. Use before answering architecture questions about "
                   "the project, and after larger file changes.")
    private = True
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        pid = getattr(ctx, "project_id", None)
        if not pid:
            return ToolResult(status="error", result=None, error=(
                "no project bound to this run — graphs are per project. "
                "Ask the user to promote the chat to a project first."))
        ok, msg = runner.start_build(_projects_dir(ctx), ctx.owner, pid, ctx.config)
        if not ok:
            return ToolResult(status="error", result=None, error=msg)
        return ToolResult(status="ok", result={
            "started": True,
            "note": "build runs in the background — poll graph.status until "
                    "state is 'ready', then use graph.query/graph.explain."})


class GraphStatus(Tool):
    name = "graph.status"
    description = ("Status of the current project's graph: state "
                   "(none/building/ready/error), node/edge counts, whether it "
                   "is stale (files changed since the build), last error.")
    private = True
    read_only = True
    poll_safe = True
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        pid = getattr(ctx, "project_id", None)
        if not pid:
            return ToolResult(status="error", result=None, error="no project bound to this run")
        return ToolResult(status="ok", result=runner.read_status(
            _projects_dir(ctx), ctx.owner, pid))


class GraphQuery(Tool):
    name = "graph.query"
    description = ("Ask a plain-language question against the current project's "
                   "project graph (e.g. 'what connects auth to the database?'). "
                   "Returns a scoped subgraph as NODE/EDGE lines — prefer this "
                   "over reading whole files for architecture questions.")
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Plain-language question."},
            "budget": {"type": "integer", "default": 1500, "minimum": 200,
                       "maximum": 8000,
                       "description": "Output token cap (smaller = more focused)."},
        },
        "required": ["question"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        got = _require_project(ctx)
        if isinstance(got, ToolResult):
            return got
        root, _ = got
        return await _query_cli(root / "graph.json", "query",
                                str(args.get("question") or ""),
                                "--budget", str(int(args.get("budget") or 1500)))


class GraphExplain(Tool):
    name = "graph.explain"
    description = ("Explain one concept/symbol of the current project: what it "
                   "is, where it's defined, and everything it connects to. "
                   "Cheaper and more complete than reading the file.")
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "node": {"type": "string",
                     "description": "Concept or symbol name (e.g. 'RateLimiter')."},
        },
        "required": ["node"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        got = _require_project(ctx)
        if isinstance(got, ToolResult):
            return got
        root, _ = got
        return await _query_cli(root / "graph.json", "explain",
                                str(args.get("node") or ""))


class GraphPath(Tool):
    name = "graph.path"
    description = ("Trace how two concepts in the current project connect "
                   "(shortest path through the graph). Use to answer 'how does "
                   "X reach Y' questions without reading intermediate files.")
    private = True
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "description": "Start concept/symbol."},
            "b": {"type": "string", "description": "End concept/symbol."},
        },
        "required": ["a", "b"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        got = _require_project(ctx)
        if isinstance(got, ToolResult):
            return got
        root, _ = got
        return await _query_cli(root / "graph.json", "path",
                                str(args.get("a") or ""), str(args.get("b") or ""))
