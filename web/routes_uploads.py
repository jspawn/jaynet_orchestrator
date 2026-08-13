"""Upload and run-output download routes (split out of web/server.py)."""

from __future__ import annotations

import os
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from runtime.outputs import deliverable_path, read_manifest
from web.ctx import _PREVIEW_MEDIA, _classify, _safe_name, _sandbox_headers, read_capped


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
        (d / stored).write_bytes(raw)
        return {"id": stored, "name": safe, "kind": _classify(safe), "size": len(raw)}

    @app.get("/api/upload/{att_id}")
    async def get_upload(att_id: str, request: Request):
        p = _resolve_attachment(request, att_id)
        if not p:
            raise HTTPException(status_code=404, detail="not found")
        # Uploaded markup is untrusted and served same-origin: sandbox HTML/SVG.
        return FileResponse(str(p), headers=_sandbox_headers(p.name))

    @app.get("/api/output/{run_id}")
    async def get_output(run_id: str, request: Request, inline: int = 0):
        m = read_manifest(outputs_dir, run_id)
        if not m or m.get("owner") != _owner(request):
            raise HTTPException(status_code=404, detail="not found")
        p = deliverable_path(outputs_dir, run_id, m)
        if not p.is_file():
            raise HTTPException(status_code=404, detail="not found")
        # Inline (preview in a new tab): omit the attachment filename and send a
        # real media type so the browser renders it. Archives are never inlined.
        if inline and m.get("kind") != "targz":
            media = _PREVIEW_MEDIA.get(os.path.splitext(p.name.lower())[1],
                                       "application/octet-stream")
            headers = {"Content-Disposition": "inline"}
            if media.split(";")[0] in ("text/html", "image/svg+xml"):
                # Agent-produced markup is untrusted and served same-origin:
                # render it, but sandbox it so no script can execute here.
                headers["Content-Security-Policy"] = "sandbox"
            return FileResponse(str(p), media_type=media, headers=headers)
        media = "application/gzip" if m.get("kind") == "targz" else "application/octet-stream"
        return FileResponse(str(p), media_type=media, filename=m["name"])
