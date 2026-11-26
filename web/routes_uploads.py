"""Upload and run-output download routes (split out of web/server.py)."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from runtime.outputs import deliverable_path, read_manifest
from web.ctx import _classify, _preview_response, _safe_name, read_capped


def register(app, s):
    max_upload_mb = s.max_upload_mb
    outputs_dir = s.outputs_dir
    _owner = s._owner
    _owner_dir = s._owner_dir
    _resolve_attachment = s._resolve_attachment

    # ---- uploads ----
    @app.post("/api/upload")
    async def upload(request: Request, filename: str = ""):
        limit = max_upload_mb * 1024 * 1024
        # Streamed read with a running counter: rejects on a declared
        # Content-Length BEFORE reading, and aborts chunked bodies (no length
        # to pre-check) as soon as they pass the cap.
        detail = f"file exceeds {max_upload_mb} MB limit"
        raw = await read_capped(request, limit, detail)
        if not raw:
            raise HTTPException(status_code=400, detail="empty upload")
        safe = _safe_name(filename)
        stored = f"{uuid.uuid4().hex[:8]}-{safe}"
        d = _owner_dir(request)
        d.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread((d / stored).write_bytes, raw)
        return {"id": stored, "name": safe, "kind": _classify(safe), "size": len(raw)}

    @app.get("/api/upload/{att_id}")
    async def get_upload(att_id: str, request: Request):
        p = _resolve_attachment(request, att_id)
        if not p:
            raise HTTPException(status_code=404, detail="not found")
        # Uploaded markup is untrusted and served same-origin. Only explicit
        # preview media may render; unknown markup-capable suffixes download.
        return _preview_response(p)

    @app.get("/api/output/{run_id}")
    async def get_output(run_id: str, request: Request, inline: int = 0):
        m = read_manifest(outputs_dir, run_id)
        if not m or m.get("owner") != _owner(request):
            raise HTTPException(status_code=404, detail="not found")
        p = deliverable_path(outputs_dir, run_id, m)
        if not p.is_file():
            raise HTTPException(status_code=404, detail="not found")
        # Inline previews use the same explicit allowlist as workspaces/uploads.
        if inline and m.get("kind") != "targz":
            return _preview_response(p, download_name=m["name"])
        media = "application/gzip" if m.get("kind") == "targz" else "application/octet-stream"
        return FileResponse(str(p), media_type=media, filename=m["name"])
