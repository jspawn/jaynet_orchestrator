"""Admin connector routes — the management surface for connector packages.

Connectors are DECLARATIVE (YAML, no code) — that's what makes them safe to
share. State (enabled / RO-RW / settings) lives in runtime/connectors.py and
applies hot: every mutation ends in connectors.refresh(), which swaps the
tool registry without a restart.
"""

from __future__ import annotations

import logging
import re
import shutil

from fastapi import HTTPException
from fastapi.responses import Response

from runtime import connectors, jaypack, paths

log = logging.getLogger(__name__)

_ID_OK = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def register(app, s):
    runtime = s.runtime

    def _rows() -> list[dict]:
        rows = connectors.refresh(runtime.registry)
        runtime.connector_rows = rows
        return rows

    def _row(cid: str) -> dict:
        if not _ID_OK.match(cid):
            raise HTTPException(status_code=400, detail="bad connector id")
        for r in _rows():
            if r.get("id") == cid:
                return r
        raise HTTPException(status_code=404, detail=f"no connector '{cid}'")

    @app.get("/api/admin/connectors")
    async def conn_list():
        rows = _rows()
        return {"connectors": [r for r in rows if "id" in r],
                "errors": [e for r in rows if "errors" in r
                           for e in r["errors"]]}

    @app.put("/api/admin/connectors/{cid}")
    async def conn_put(cid: str, body: dict):
        row = _row(cid)
        enabled = body.get("enabled")
        mode = body.get("mode")
        settings = body.get("settings")
        if mode is not None:
            if mode not in ("ro", "rw"):
                raise HTTPException(status_code=400,
                                    detail="mode must be 'ro' or 'rw'")
            if mode == "rw" and row["allows"] != "rw":
                raise HTTPException(
                    status_code=400,
                    detail=f"connector '{cid}' is a read-only package "
                           "(allows: ro) — RW would exceed its ceiling")
        if settings is not None:
            if not isinstance(settings, dict):
                raise HTTPException(status_code=400,
                                    detail="settings must be a mapping")
            unknown = set(settings) - set(row["settings_schema"])
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown settings: {', '.join(sorted(unknown))} "
                           f"(schema: {', '.join(sorted(row['settings_schema'])) or '—'})")
        connectors.set_state(cid, enabled=enabled, mode=mode,
                             settings=settings)
        return {"connector": _row(cid)}

    @app.post("/api/admin/connectors/{cid}/test")
    async def conn_test(cid: str):
        row = _row(cid)
        if not row["enabled"]:
            raise HTTPException(status_code=400,
                                detail="connector is disabled")
        # Probe with the first read-only tool, args from param defaults —
        # testing must never mutate the target system.
        name = next((n for n in row["tool_names"]
                     if (runtime.registry.get(n) is not None
                         and not runtime.registry.get(n).write)), None)
        if name is None:
            raise HTTPException(
                status_code=400,
                detail="no read-only tool live to probe with (mode is ro "
                       "and all tools write?)")
        tool = runtime.registry.get(name)
        args = {k: v["default"] for k, v in
                (tool.parameters.get("properties") or {}).items()
                if "default" in v}
        missing = [p for p in (tool.parameters.get("required") or [])
                   if p not in args]
        if missing:
            return {"ok": False, "tool": name,
                    "error": f"probe needs args without defaults: "
                             f"{', '.join(missing)} — call {name} in chat "
                             "with real values instead"}
        r = await tool.execute(args, None)
        return {"ok": r.status == "ok", "tool": name,
                "result": str(r.result or "")[:400] if r.status == "ok"
                else None,
                "error": r.error}

    @app.get("/api/admin/connectors/{cid}/export")
    async def conn_export(cid: str):
        row = _row(cid)
        try:
            data = jaypack.build_pack(
                "connector", cid,
                description=row["description"])
        except jaypack.JaypackError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return Response(
            content=data, media_type="application/zip",
            headers={"Content-Disposition":
                     f'attachment; filename="{cid}.jayconn"'})

    @app.delete("/api/admin/connectors/{cid}")
    async def conn_delete(cid: str):
        _row(cid)
        target = paths.CUSTOM_CONN_DIR / cid
        f = paths.CUSTOM_CONN_DIR / f"{cid}.yaml"
        if target.is_dir():
            shutil.rmtree(target)
        elif f.is_file():
            f.unlink()
        else:
            raise HTTPException(status_code=404,
                                detail="connector source not found")
        connectors.drop_state(cid)
        _rows()
        return {"ok": True, "deleted": cid}
