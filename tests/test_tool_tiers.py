"""Capability tier (audit 2026-10-11 Tier 1): Tool.admin_only is an
intrinsic, code-declared floor for lifecycle/host-control tools — enforced
at selection, dispatch and slash regardless of the
security.admin_only_tools config list (which stays the operator's way to
restrict MORE tools).

The completeness sweep below is the point of the exercise: any tool whose
module spawns host processes or drives ServeStart/ModelUse/JobStart must
either declare `admin_only = True` or carry a justified exception here —
so gaps like the procedure.save write-gate bypass fail CI instead of
shipping.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

from runtime.registry import ToolRegistry
from runtime.slash import run_slash
from runtime.tool_base import Tool, ToolContext, ToolResult
from tests.test_loop_regressions import _final, _Registry, _runtime, _tc

ROOT = Path(__file__).resolve().parent.parent

# Module-source tokens meaning "this tool can start/control host processes
# or model servers". Module-level (not class-level) on purpose: helper
# functions live outside the class body, and conservative over-capture is
# the safe direction for a completeness gate.
_DANGER_TOKENS = ("proc_run", "create_subprocess", "subprocess.", "Popen",
                  "os.system", "ServeStart", "ModelUse(", "JobStart(")

# Justified exceptions: a flagged tool that is NOT admin-tier must say why
# here. Adding a process-spawning tool forces this decision at PR time.
_SPAWN_OK = {
    # Sandboxed executors — the subprocess IS the sandboxed payload.
    "code.run": "sandboxed executor (firejail prefix, network off)",
    "code.execute": "hidden legacy alias of code.run — same sandbox",
    "code.patch": "workspace-confined git apply; write-gated",
    "specialist.delegate": ("child runs are workspace/worktree-confined "
                            "with caller-fixed toolsets; git + ModelUse "
                            "calls are internal to the swap policy"),
    # Workspace-scoped git mutations (write-gated / confirmation).
    "git.add": "workspace-scoped git mutation; write-gated",
    "git.branch": "workspace-scoped git mutation; write-gated",
    "git.commit": "workspace-scoped git mutation; confirmation-gated",
    # Read-only queries whose module also holds a spawner.
    "code.symbols": "read-only query (ctags)",
    "git.diff": "read-only query",
    "git.log": "read-only query",
    "git.show": "read-only query",
    "git.status": "read-only query",
    "gpu.status": "read-only query (rocm-smi)",
    "lint.run": "read-only linter run",
    "ops.status": "read-only query; shares a module with ops.run",
    "model.list": "read-only catalog query; shares a module with ModelUse",
    # Plugins (opt-in; declared per code, not per enable state).
    "bench.fetch": "benchlab plugin: local download pipeline",
    "bench.import": "benchlab plugin: local import pipeline",
    "bench.sources": "benchlab plugin: read-only query",
    "graph.build": "graphify plugin: local parse pipeline, workspace output",
    "graph.seed_kg": "graphify plugin: local parse pipeline, workspace output",
    "graph.explain": "graphify plugin: read-only query",
    "graph.path": "graphify plugin: read-only query",
    "graph.query": "graphify plugin: read-only query",
    "graph.status": "graphify plugin: read-only query",
    "browser.browse": ("h5i plugin: domain allowlist + SSRF guard; "
                       "off by default"),
    "browser.recon": ("h5i plugin: domain allowlist + SSRF guard; "
                      "off by default"),
    "browser.test": ("h5i plugin: domain allowlist + SSRF guard; "
                     "off by default"),
    "browser.websec": ("h5i plugin: allowlist + SSRF guard + "
                       "confirmation-gated active verbs; off by default"),
}


def _full_registry() -> ToolRegistry:
    """Core tools + every plugin's tools/ dir — a STATIC sweep: the tier is
    declared per code, so plugin enable state is irrelevant here."""
    reg = ToolRegistry(ROOT / "tools")
    reg.discover()
    for d in sorted((ROOT / "plugins").glob("*/tools")):
        reg.discover_extra(d)
    return reg


def _flagged(tool) -> list[str]:
    src = inspect.getsource(sys.modules[type(tool).__module__])
    return [tok for tok in _DANGER_TOKENS if tok in src]


def test_process_spawning_tools_are_admin_tier_or_justified():
    reg = _full_registry()
    problems = []
    for tool in reg.all():
        hits = _flagged(tool)
        if not hits:
            continue
        if getattr(tool, "admin_only", False):
            continue
        if tool.name in _SPAWN_OK:
            continue
        problems.append(f"{tool.name} ({', '.join(hits)})")
    assert not problems, (
        "tools spawning host processes / model servers with no admin_only "
        "declaration and no _SPAWN_OK justification:\n"
        + "\n".join(f"  - {p}" for p in problems)
        + "\nDeclare `admin_only = True` on the tool, or add a justified "
          "exception to _SPAWN_OK in tests/test_tool_tiers.py.")


def test_spawn_ok_exceptions_are_current():
    """No stale exceptions: every _SPAWN_OK entry must exist and still be
    flagged — a tool that stopped spawning loses its exception."""
    reg = _full_registry()
    by_name = {t.name: t for t in reg.all()}
    stale = [n for n in _SPAWN_OK
             if n not in by_name or not _flagged(by_name[n])]
    assert not stale, f"stale _SPAWN_OK entries: {stale}"


def test_default_floor_tools_declare_the_tier():
    """The DEFAULT_ADMIN_ONLY_TOOLS floor and the declarations must not
    drift apart: every tool matched by the floor declares admin_only."""
    from runtime.loop import DEFAULT_ADMIN_ONLY_TOOLS, _tool_policy_match
    reg = _full_registry()
    missing = [t.name for t in reg.all()
               if _tool_policy_match(t.name, DEFAULT_ADMIN_ONLY_TOOLS)
               and not getattr(t, "admin_only", False)]
    assert not missing, (f"floor-matched tools without the declaration: "
                         f"{missing}")


# ---- enforcement: a declared tier needs no config entry ------------------------

class _TieredTool(Tool):
    name = "zzz.tiered"                 # matches NO admin_only pattern
    admin_only = True
    description = "d"

    async def execute(self, args, context):
        return ToolResult(status="ok", tool_name=self.name, result="ran")


def test_declared_tier_hidden_from_selection_and_refused_at_dispatch():
    rt, _ = _runtime(_Registry(["fs.read"],
                               real={"zzz.tiered": _TieredTool()}),
                     [_tc("zzz.tiered", "{}"), _final("recovered")])
    schemas = []
    real_turn = rt._model_turn

    async def spy_turn(messages, tools_schema, **kw):
        schemas.append(tools_schema)
        return await real_turn(messages, tools_schema, **kw)
    rt._model_turn = spy_turn
    out = asyncio.run(rt.run("do it", is_admin=False))
    offered = {s["function"]["name"] for s in schemas[0]}
    assert "zzz.tiered" not in offered               # hidden at selection
    assert "zzz.tiered→error: tool 'zzz.tiered' is admin-only" \
        in out["trajectory"]                          # refused at dispatch


def test_declared_tier_still_works_for_admin():
    rt, _ = _runtime(_Registry(["fs.read"],
                               real={"zzz.tiered": _TieredTool()}),
                     [_tc("zzz.tiered", "{}"), _final("done")])
    out = asyncio.run(rt.run("do it", is_admin=True))
    assert out["status"] == "ok"
    assert "is admin-only" not in out["trajectory"]


@pytest.mark.asyncio
async def test_declared_tier_refused_on_slash_path():
    reg = _Registry([], real={"zzz.tiered": _TieredTool()})
    ctx = ToolContext(request_id="t", config={}, budget=None,
                      is_admin=False)
    out = await run_slash("/zzz.tiered", reg, ctx)
    assert "admin-only" in out
