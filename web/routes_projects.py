"""Project + per-chat scratch workspace routes (split out of web/server.py)."""

from __future__ import annotations

import os

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from runtime import hooks as _hooks
from web import projects as PJ
from web.ctx import _sandbox_headers


def register(app, s):
    projects_dir = s.projects_dir
    max_project_file_mb = s.max_project_file_mb
    _owner = s._owner
    _project_root = s._project_root
    _promote_chat_to_project = s._promote_chat_to_project
    _scratch_root = s._scratch_root
    chats = s.chats

    # ---- projects ----
    @app.post("/api/chats/{chat_id}/promote")
    async def promote_chat(chat_id: str, req: dict, request: Request):
        owner = _owner(request)
        chat = chats.get(chat_id, owner=owner)
        if not chat:
            raise HTTPException(status_code=404, detail="no such chat")
        name = ((req or {}).get("name") or chat.get("title") or "Project").strip()[:120]
        meta = _promote_chat_to_project(owner, chat, name)
        return {"project": meta, "chat_id": chat_id, "project_id": meta["id"]}

    @app.get("/api/projects")
    async def list_projects(request: Request):
        return {"projects": PJ.list_projects(projects_dir, _owner(request))}

    @app.post("/api/projects")
    async def create_project(req: dict, request: Request):
        name = (req or {}).get("name", "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name required")
        return PJ.create_project(projects_dir, _owner(request), name)

    @app.delete("/api/projects/{pid}")
    async def delete_project(pid: str, request: Request):
        owner = _owner(request)
        safe_pid = os.path.basename(pid)
        if not PJ.delete_project(projects_dir, owner, safe_pid):
            raise HTTPException(status_code=404, detail="no such project")
        _hooks.fire("on_project_delete", owner, safe_pid)
        return {"ok": True, "deleted": pid}

    @app.get("/api/projects/{pid}/files")
    async def project_files(pid: str, request: Request):
        meta, root = _project_root(request, pid)
        if not meta:
            raise HTTPException(status_code=404, detail="no such project")
        return {"project": meta, "root": str(root), "entries": PJ.tree(root)}

    @app.get("/api/projects/{pid}/file")
    async def project_read_file(pid: str, request: Request, path: str):
        meta, root = _project_root(request, pid)
        if not meta:
            raise HTTPException(status_code=404, detail="no such project")
        out = PJ.read_file(root, path)
        if out is None:
            raise HTTPException(status_code=404, detail="no such file")
        return out

    @app.get("/api/projects/{pid}/download")
    async def project_download_file(pid: str, request: Request, path: str,
                                    inline: bool = False):
        meta, root = _project_root(request, pid)
        if not meta:
            raise HTTPException(status_code=404, detail="no such project")
        p = PJ.safe_join(root, path)
        if p is None or not p.is_file():
            raise HTTPException(status_code=404, detail="no such file")
        if inline:
            # in-browser preview (e.g. <img> in the file explorer): media type
            # guessed from the suffix, no forced attachment disposition. HTML/SVG
            # are untrusted same-origin markup — served sandboxed (no script).
            return FileResponse(str(p), headers=_sandbox_headers(p.name))
        return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")

    @app.put("/api/projects/{pid}/file")
    async def project_write_file(pid: str, request: Request, path: str):
        meta, root = _project_root(request, pid)
        if not meta:
            raise HTTPException(status_code=404, detail="no such project")
        limit = max_project_file_mb * 1024 * 1024
        # Reject on a declared Content-Length BEFORE reading the body; the
        # post-read check below stays as the fallback for chunked requests.
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > limit:
            raise HTTPException(status_code=413, detail="file too large")
        body = await request.body()
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="file too large")
        out = PJ.write_file(root, path, body)
        if out is None:
            raise HTTPException(status_code=400, detail="invalid path")
        _hooks.fire("on_project_file_changed", _owner(request),
                    os.path.basename(pid), path, projects_dir)
        return out

    @app.delete("/api/projects/{pid}/file")
    async def project_delete_file(pid: str, request: Request, path: str):
        meta, root = _project_root(request, pid)
        if not meta:
            raise HTTPException(status_code=404, detail="no such project")
        if not PJ.delete_path(root, path):
            raise HTTPException(status_code=404, detail="no such file")
        _hooks.fire("on_project_file_changed", _owner(request),
                    os.path.basename(pid), path, projects_dir)
        return {"ok": True, "deleted": path}

    @app.post("/api/projects/{pid}/mkdir")
    async def project_mkdir(pid: str, req: dict, request: Request):
        meta, root = _project_root(request, pid)
        if not meta:
            raise HTTPException(status_code=404, detail="no such project")
        out = PJ.mkdir(root, (req or {}).get("path", ""))
        if out is None:
            raise HTTPException(status_code=400, detail="invalid path")
        return out

    @app.post("/api/projects/{pid}/rename")
    async def project_rename(pid: str, req: dict, request: Request):
        meta, root = _project_root(request, pid)
        if not meta:
            raise HTTPException(status_code=404, detail="no such project")
        out = PJ.move_path(root, (req or {}).get("from", ""), (req or {}).get("to", ""))
        if out is None:
            raise HTTPException(status_code=400,
                                detail="rename failed: bad path, missing source, or destination exists")
        # A rename touches both paths — fire for source and destination so
        # hook consumers tracking per-path state see the complete picture.
        for p in ((req or {}).get("from", ""), (req or {}).get("to", "")):
            _hooks.fire("on_project_file_changed", _owner(request),
                        os.path.basename(pid), p, projects_dir)
        return out

    # ---- per-chat scratch workspace (the agent's work_root when no project) ----
    # Mirrors the project file API but rooted at the owner-scoped chat scratch
    # dir. This is what the "no project, showing current chat" panel browses, so
    # files the agent writes with fs.* in a bare chat are visible and editable.
    @app.get("/api/chat-scratch/{cid}/files")
    async def scratch_files(cid: str, request: Request):
        root = _scratch_root(_owner(request), cid, create=False)
        if root is None or not root.is_dir():
            return {"entries": []}
        return {"root": str(root), "entries": PJ.tree(root)}

    @app.get("/api/chat-scratch/{cid}/file")
    async def scratch_read_file(cid: str, request: Request, path: str):
        root = _scratch_root(_owner(request), cid, create=False)
        if root is None:
            raise HTTPException(status_code=404, detail="no such file")
        out = PJ.read_file(root, path)
        if out is None:
            raise HTTPException(status_code=404, detail="no such file")
        return out

    @app.get("/api/chat-scratch/{cid}/download")
    async def scratch_download_file(cid: str, request: Request, path: str,
                                    inline: bool = False):
        root = _scratch_root(_owner(request), cid, create=False)
        if root is None:
            raise HTTPException(status_code=404, detail="no such file")
        p = PJ.safe_join(root, path)
        if p is None or not p.is_file():
            raise HTTPException(status_code=404, detail="no such file")
        if inline:
            return FileResponse(str(p), headers=_sandbox_headers(p.name))
        return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")

    @app.put("/api/chat-scratch/{cid}/file")
    async def scratch_write_file(cid: str, request: Request, path: str):
        root = _scratch_root(_owner(request), cid)
        limit = max_project_file_mb * 1024 * 1024
        # Reject on a declared Content-Length BEFORE reading the body; the
        # post-read check below stays as the fallback for chunked requests.
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > limit:
            raise HTTPException(status_code=413, detail="file too large")
        body = await request.body()
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="file too large")
        out = PJ.write_file(root, path, body)
        if out is None:
            raise HTTPException(status_code=400, detail="invalid path")
        return out

    @app.delete("/api/chat-scratch/{cid}/file")
    async def scratch_delete_file(cid: str, request: Request, path: str):
        root = _scratch_root(_owner(request), cid, create=False)
        if root is None or not PJ.delete_path(root, path):
            raise HTTPException(status_code=404, detail="no such file")
        return {"ok": True, "deleted": path}

    @app.post("/api/chat-scratch/{cid}/mkdir")
    async def scratch_mkdir(cid: str, req: dict, request: Request):
        root = _scratch_root(_owner(request), cid)
        out = PJ.mkdir(root, (req or {}).get("path", ""))
        if out is None:
            raise HTTPException(status_code=400, detail="invalid path")
        return out

    @app.post("/api/chat-scratch/{cid}/rename")
    async def scratch_rename(cid: str, req: dict, request: Request):
        root = _scratch_root(_owner(request), cid)
        out = PJ.move_path(root, (req or {}).get("from", ""), (req or {}).get("to", ""))
        if out is None:
            raise HTTPException(status_code=400,
                                detail="rename failed: bad path, missing source, or destination exists")
        return out
