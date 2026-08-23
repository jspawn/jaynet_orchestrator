"""Benchlab plugin routes — the admin API behind the plugin's UI (ui/).

Convention (web/routes_plugins.py): plugin admin APIs live under
/api/admin/plugins/<name>/api/, so the auth middleware's /api/admin gate
applies — no per-route checks here. The endpoints reuse the bench.* tool
classes directly (their execute() never touches ctx); long operations
(fetch/import) run as ONE background task with a pollable job status.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from fastapi import HTTPException, Request


def _load_bench():
    """Import the plugin's tools/bench.py ONCE per process (plugin modules are
    loaded via spec_from_file_location, not as a package)."""
    name = "benchlab_plugin_tools"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parent / "tools" / "bench.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


bench = _load_bench()

# Single in-memory job: one admin, one operation at a time. state is one of
# idle | running | done | error.
_JOB: dict = {"state": "idle", "op": None, "result": None, "error": None}


def register(app, s):

    @app.get("/api/admin/plugins/benchlab/api/overview")
    async def overview():
        res = await bench.BenchSources().execute({}, None)
        if res.status != "ok":
            raise HTTPException(status_code=500, detail=res.error)
        return res.result

    @app.get("/api/admin/plugins/benchlab/api/job")
    async def job():
        return _JOB

    async def _run(op: str, coro):
        if _JOB["state"] == "running":
            raise HTTPException(status_code=409,
                                detail="a benchlab operation is already running")
        _JOB.update(state="running", op=op, result=None, error=None)

        async def work():
            try:
                res = await coro
                if res.status == "ok":
                    _JOB.update(state="done", result=res.result)
                else:
                    _JOB.update(state="error", error=res.error)
            except Exception as e:
                _JOB.update(state="error", error=f"{type(e).__name__}: {e}")

        asyncio.create_task(work())
        return {"started": True, "op": op}

    @app.post("/api/admin/plugins/benchlab/api/fetch")
    async def fetch_catalog():
        return await _run("fetch", bench.BenchFetch().execute(
            {"source": "terminal-bench"}, None))

    @app.post("/api/admin/plugins/benchlab/api/import")
    async def import_cases(request: Request):
        body = (await request.json()) or {}
        source = body.get("source")
        if source not in ("terminal-bench", "gaia"):
            raise HTTPException(status_code=400,
                                detail="source must be terminal-bench or gaia")
        args: dict = {"source": source}
        if body.get("mode") is not None:
            args["mode"] = str(body["mode"])
        if isinstance(body.get("limit"), int):
            args["limit"] = body["limit"]
        if body.get("tasks"):
            args["tasks"] = [str(t) for t in body["tasks"]]
        return await _run(f"import-{source}",
                          bench.BenchImport().execute(args, None))
