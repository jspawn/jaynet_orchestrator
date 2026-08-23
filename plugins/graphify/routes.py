"""Graphify plugin routes — per-project graph build/status/viz endpoints.

Same register(app, state) contract as core route modules; registered LAST
(after core routes) by web/server.py for loaded plugins only. Owner scoping
mirrors web/routes_projects.py: a user only ever reaches their own projects.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse


def _load_runner():
    """Shared runner module — MUST reuse the single sys.modules entry
    (see tools/graph.py:_load_runner); a fresh exec would split the _jobs
    dict and break the duplicate-build guard / cancel-on-delete."""
    name = "graphify_plugin_runner"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parent / "runner.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


runner = _load_runner()


def register(app, s):
    projects_dir = s.projects_dir
    _owner = s._owner
    runtime = s.runtime
    # Stash the live config for the on_project_file_changed hook — it gets
    # no config argument, and auto-rebuild gating/keywords live there.
    runner.set_config(runtime.config)

    def _check(request: Request, pid: str) -> tuple[str, str]:
        """(owner, safe_pid) — 404 unless the project exists for this user."""
        owner = _owner(request)
        safe_pid = os.path.basename(pid)
        proj = runner.project_dir(projects_dir, owner, safe_pid)
        if not (proj / "project.json").is_file():
            raise HTTPException(status_code=404, detail="no such project")
        return owner, safe_pid

    @app.post("/api/projects/{pid}/graph/build")
    async def graph_build(pid: str, request: Request):
        owner, safe_pid = _check(request, pid)
        ok, msg = runner.start_build(projects_dir, owner, safe_pid, runtime.config)
        if not ok:
            raise HTTPException(status_code=409, detail=msg)
        return {"ok": True, "note": msg}

    @app.get("/api/projects/{pid}/graph/status")
    async def graph_status(pid: str, request: Request):
        owner, safe_pid = _check(request, pid)
        return runner.read_status(projects_dir, owner, safe_pid)

    @app.post("/api/projects/{pid}/graph/cancel")
    async def graph_cancel(pid: str, request: Request):
        owner, safe_pid = _check(request, pid)
        stopped = runner.cancel_build(owner, safe_pid)
        if stopped:
            runner.write_status(projects_dir, owner, safe_pid,
                                state="error", error="cancelled by user")
        return {"ok": stopped}

    @app.get("/api/projects/{pid}/graph/viz")
    async def graph_viz(pid: str, request: Request):
        owner, safe_pid = _check(request, pid)
        html = runner.graph_root(projects_dir, owner, safe_pid) / "graph.html"
        if not html.is_file():
            raise HTTPException(status_code=404, detail="no graph viz — build first")
        # Interactive viz NEEDS scripts, but it is graph-generated content
        # (node labels come from project files) — run it in an opaque origin
        # (no cookies, no same-origin access to JayNet). The viz is
        # self-contained, so it works fully sandboxed.
        return FileResponse(str(html), headers={
            "Content-Security-Policy": "sandbox allow-scripts",
            "X-Content-Type-Options": "nosniff"})

    @app.get("/api/projects/{pid}/graph/report")
    async def graph_report(pid: str, request: Request):
        owner, safe_pid = _check(request, pid)
        md = runner.graph_root(projects_dir, owner, safe_pid) / "GRAPH_REPORT.md"
        if not md.is_file():
            raise HTTPException(status_code=404, detail="no report — build first")
        return PlainTextResponse(
            md.read_text(encoding="utf-8", errors="replace"),
            media_type="text/markdown",
            headers={"X-Content-Type-Options": "nosniff"})
