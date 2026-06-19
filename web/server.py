"""FastAPI + SSE service: chat with the orchestrator and watch the loop live.

Phase 9 + auth/admin:
- The AgentRuntime is created once and lives for the process.
- POST /api/chat starts a run as a background task and returns its run_id; the run
  streams transport-neutral events into an EventBus; GET /api/stream/{run_id} is
  the SSE feed. Approvals/cancels flow back over plain POSTs.
- Access is gated by login: HMAC-signed session cookies (see web/auth.py), with an
  optional ORCH_WEB_TOKEN bearer for API/CLI. Per-user tool toggles persist in the
  user store and are applied to each run. Admins get /admin: edit the system
  prompt, see service status, and read run logs/users.

Nothing here leaks into runtime/loop.py — the loop only knows `on_event` and
`confirm_provider`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from runtime.confirm import WebConfirmationProvider
from runtime.events import EventBus
from runtime.loop import AgentRuntime
from runtime.outputs import (deliverable_path, delete_output, mark_saved,
                             read_manifest, sweep)
from web.auth import LoginThrottle, UserStore, read_session, resolve_secret, sign_session
from web.store import ChatStore
from web import projects as PJ

_STATIC = Path(__file__).parent / "static"
_FORGET_AFTER_S = 120
_COOKIE = "jaynet_session"
_OPEN_PATHS = {"/login", "/api/login", "/api/health", "/favicon.ico"}

# Upload classification. Images can't be seen by the local text brain, so they're
# stored and offered to vision-capable models/tools; text is inlined into the
# message; everything else is noted by path for a tool to inspect.
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff"}
_TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
             ".toml", ".ini", ".cfg", ".conf", ".log", ".xml", ".html", ".htm",
             ".css", ".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".bash", ".sql",
             ".rs", ".go", ".c", ".h", ".cpp", ".java", ".rb", ".php", ".lua",
             ".tex", ".rst", ".env", ".gitignore", ".dockerfile"}
_MAX_INLINE_CHARS = 12000   # cap inlined text so one upload can't blow the context


def _safe_name(name: str) -> str:
    """Reduce an arbitrary client filename to a single safe path component."""
    name = os.path.basename((name or "").strip().replace("\\", "/").split("/")[-1])
    out = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in name).strip(". ")
    return (out or "file")[:120]


def _classify(filename: str) -> str:
    ext = os.path.splitext(filename.lower())[1]
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _TEXT_EXT:
        return "text"
    return "binary"


class ChatRequest(BaseModel):
    message: str
    share_private: bool = False
    auto_confirm: bool = False
    think: bool = True                       # Qwen3 chain-of-thought on/off
    tools: list[str] | None = None
    budget_overrides: dict | None = None
    history: list[dict] | None = None
    attachments: list[str] | None = None   # uploaded file ids (owner-scoped)
    project_id: str | None = None           # work inside this project's files


class ApproveRequest(BaseModel):
    confirmation_id: str
    approved: bool


class TurnModel(BaseModel):
    user_message: str
    answer: str = ""
    run_id: str | None = None
    status: str | None = None
    events: list[dict] | None = None


class SaveChatRequest(BaseModel):
    id: str | None = None
    title: str | None = None
    turns: list[TurnModel]
    project_id: str | None = None


class RenameRequest(BaseModel):
    title: str


class LoginRequest(BaseModel):
    username: str
    password: str
    code: str | None = None


class TwoFACodeRequest(BaseModel):
    code: str


class ToolsRequest(BaseModel):
    disabled: list[str]


class PromptRequest(BaseModel):
    content: str


class NewUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class PasswordRequest(BaseModel):
    password: str


class AdminFlagRequest(BaseModel):
    is_admin: bool


def create_app(config_path: str = "/srv/orchestrator/config/runtime.yaml") -> FastAPI:
    app = FastAPI(title="JayNet Orchestrator")
    runtime = AgentRuntime(config_path)
    bus = EventBus()
    pending: dict[tuple[str, str], asyncio.Future] = {}
    conf_cfg = runtime.config.get("confirmation", {}) or {}
    provider = WebConfirmationProvider(
        pending,
        timeout_s=float(conf_cfg.get("web_timeout_s", 300)),
        on_timeout=(conf_cfg.get("web_on_timeout", "deny") == "allow"),
    )
    tasks: dict[str, asyncio.Task] = {}
    run_owner: dict[str, str | None] = {}   # run_id -> owner, for access checks
    throttle = LoginThrottle()
    token = os.environ.get("ORCH_WEB_TOKEN")
    web_cfg = runtime.config.get("web", {}) or {}
    data_dir = Path(web_cfg.get("chats_db", "/srv/orchestrator/data/chats.db")).parent
    chats = ChatStore(web_cfg.get("chats_db", "/srv/orchestrator/data/chats.db"))
    users = UserStore(web_cfg.get("users_db", str(data_dir / "users.db")))
    secret = resolve_secret(data_dir)
    cookie_secure = bool(web_cfg.get("cookie_secure", False))
    uploads_dir = Path(web_cfg.get("uploads_dir", str(data_dir / "uploads")))
    max_upload_mb = int(web_cfg.get("max_upload_mb", 25))
    outputs_dir = Path(web_cfg.get("outputs_dir", str(data_dir / "outputs")))
    output_ttl_hours = float(web_cfg.get("output_ttl_hours", 24))
    max_output_mb = int(web_cfg.get("max_output_mb", 200))
    projects_dir = Path(web_cfg.get("projects_dir", str(data_dir / "projects")))
    projects_dir.mkdir(parents=True, exist_ok=True)
    max_project_file_mb = int(web_cfg.get("max_project_file_mb", 25))
    _sweep_state = {"last": 0.0}
    try:
        sweep(outputs_dir, output_ttl_hours)   # clean orphans/expired on boot
    except Exception:
        pass
    started_at = time.time()

    app.state.runtime = runtime
    app.state.bus = bus
    app.state.pending = pending
    app.state.tasks = tasks
    app.state.provider = provider
    app.state.chats = chats
    app.state.users = users

    def _user(request: Request) -> dict:
        return getattr(request.state, "user", None) or {"username": "_token", "is_admin": True}

    def _owner(request: Request) -> str | None:
        u = _user(request)
        return None if u["username"] == "_token" else u["username"]

    def _owner_dir(request: Request) -> Path:
        return uploads_dir / (_owner(request) or "_token")

    def _resolve_attachment(request: Request, att_id: str) -> Path | None:
        """Map a client-supplied attachment id to a trusted on-disk path, scoped
        to the caller's own upload dir. Basename-only + containment check defeat
        path traversal; we never trust a client-sent path."""
        base = _owner_dir(request).resolve()
        p = (base / os.path.basename(att_id or "")).resolve()
        if p.is_file() and base in p.parents:
            return p
        return None

    def _can_access_run(request: Request, run_id: str) -> bool:
        """A run is accessible to its owner, or to an admin/token session.
        Unknown run_id -> False (treated as 404 to avoid leaking existence)."""
        if run_id not in run_owner:
            return False
        u = _user(request)
        return bool(u["is_admin"]) or run_owner[run_id] == _owner(request)

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        path = request.url.path
        if path in _OPEN_PATHS or path.startswith("/static"):
            return await call_next(request)
        username = read_session(request.cookies.get(_COOKIE), secret)
        user = users.get(username) if username else None
        if user is None and token and request.headers.get("authorization") == f"Bearer {token}":
            user = {"username": "_token", "is_admin": True}
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return RedirectResponse("/login", status_code=302)
        if (path == "/admin" or path.startswith("/api/admin")) and not user["is_admin"]:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "forbidden"}, status_code=403)
            return RedirectResponse("/", status_code=302)
        request.state.user = user
        return await call_next(request)

    # ---- pages ----
    # Serve bundled static assets (e.g. the vendored CodeMirror editor) from
    # /static. Open (no auth) — these are just front-end libraries.
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(_STATIC / "index.html")

    @app.get("/login")
    async def login_page():
        return FileResponse(_STATIC / "login.html")

    @app.get("/admin")
    async def admin_page():
        return FileResponse(_STATIC / "admin.html")

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    # ---- auth API ----
    @app.post("/api/login")
    async def login(req: LoginRequest):
        key = f"u:{req.username}"
        ra = throttle.retry_after(key)
        if ra:
            return JSONResponse({"detail": "too many attempts; try again later"},
                                status_code=429, headers={"Retry-After": str(ra)})
        u = users.verify(req.username, req.password)
        if not u:
            throttle.record_failure(key)
            raise HTTPException(status_code=401, detail="invalid credentials")
        if users.has_totp(req.username):
            if not req.code:
                # distinct code so the UI can reveal the 2FA field (not a failure)
                raise HTTPException(status_code=401, detail="totp_required")
            if not users.verify_second_factor(req.username, req.code):
                throttle.record_failure(key)
                raise HTTPException(status_code=401, detail="invalid two-factor code")
        throttle.record_success(key)
        resp = JSONResponse({"ok": True, "username": u["username"], "is_admin": u["is_admin"]})
        resp.set_cookie(_COOKIE, sign_session(u["username"], secret),
                        httponly=True, samesite="lax", secure=cookie_secure,
                        max_age=7 * 24 * 3600, path="/")
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(_COOKIE, path="/")
        return resp

    @app.get("/api/me")
    async def me(request: Request):
        u = _user(request)
        twofa = False if u["username"] == "_token" else users.has_totp(u["username"])
        return {"username": u["username"], "is_admin": u["is_admin"], "twofa": twofa}

    # ---- two-factor (self-service) ----
    @app.get("/api/2fa/status")
    async def twofa_status(request: Request):
        u = _user(request)
        if u["username"] == "_token":
            return {"enabled": False, "backup_remaining": 0}
        return {"enabled": users.has_totp(u["username"]),
                "backup_remaining": users.backup_codes_remaining(u["username"])}

    @app.post("/api/2fa/setup")
    async def twofa_setup(request: Request):
        u = _user(request)
        if u["username"] == "_token":
            raise HTTPException(status_code=400, detail="token session cannot enroll")
        if users.has_totp(u["username"]):
            raise HTTPException(status_code=409, detail="2FA already enabled")
        res = users.start_enrollment(u["username"])
        if not res:
            raise HTTPException(status_code=404, detail="no such user")
        return res  # {secret, otpauth_uri}

    @app.post("/api/2fa/confirm")
    async def twofa_confirm(req: TwoFACodeRequest, request: Request):
        u = _user(request)
        if u["username"] == "_token":
            raise HTTPException(status_code=400, detail="token session cannot enroll")
        codes = users.confirm_enrollment(u["username"], req.code)
        if codes is None:
            raise HTTPException(status_code=400, detail="invalid or expired code")
        return {"ok": True, "backup_codes": codes}  # shown once

    @app.post("/api/2fa/disable")
    async def twofa_disable(req: TwoFACodeRequest, request: Request):
        u = _user(request)
        if u["username"] == "_token":
            raise HTTPException(status_code=400, detail="token session")
        if not users.has_totp(u["username"]):
            return {"ok": True}
        if not users.verify_second_factor(u["username"], req.code):
            raise HTTPException(status_code=401, detail="invalid two-factor code")
        users.disable_totp(u["username"])
        return {"ok": True}

    # ---- uploads ----
    @app.post("/api/upload")
    async def upload(request: Request, filename: str = ""):
        raw = await request.body()
        if len(raw) > max_upload_mb * 1024 * 1024:
            raise HTTPException(status_code=413,
                                detail=f"file exceeds {max_upload_mb} MB limit")
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
        return FileResponse(str(p))

    @app.get("/api/output/{run_id}")
    async def get_output(run_id: str, request: Request):
        m = read_manifest(outputs_dir, run_id)
        if not m or m.get("owner") != _owner(request):
            raise HTTPException(status_code=404, detail="not found")
        p = deliverable_path(outputs_dir, run_id, m)
        if not p.is_file():
            raise HTTPException(status_code=404, detail="not found")
        media = "application/gzip" if m.get("kind") == "targz" else "application/octet-stream"
        return FileResponse(str(p), media_type=media, filename=m["name"])

    def _augment_with_attachments(request: Request, message: str,
                                  attachment_ids: list[str] | None) -> str:
        """Append trusted, server-resolved attachment context to the user message.
        Text is inlined (bounded); images/binaries are noted by path so the agent
        can route them to a vision model or inspect them with a tool."""
        if not attachment_ids:
            return message
        blocks = []
        for att_id in attachment_ids:
            p = _resolve_attachment(request, att_id)
            if not p:
                continue
            name = p.name.split("-", 1)[1] if "-" in p.name else p.name
            kind = _classify(p.name)
            size = p.stat().st_size
            if kind == "text":
                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                except Exception as e:
                    content = f"[could not read: {e}]"
                if len(content) > _MAX_INLINE_CHARS:
                    content = content[:_MAX_INLINE_CHARS] + "\n…[truncated]"
                blocks.append(f"- {name} (text, {size} B) saved at {p}\n"
                              f"  --- begin {name} ---\n{content}\n  --- end {name} ---")
            elif kind == "image":
                blocks.append(f"- {name} (image, {size} B) saved at {p}\n"
                              "  The local brain cannot view images; to analyse it, "
                              "pass this path to a vision-capable model via llm.call, "
                              "or use an image tool.")
            else:
                blocks.append(f"- {name} (binary, {size} B) saved at {p}\n"
                              "  If a skill applies (e.g. the docx skill for Word "
                              "files), load it; otherwise use an appropriate tool "
                              "(e.g. fs.read for text-like content) to inspect it.")
        if not blocks:
            return message
        return message + "\n\n[Attached files]\n" + "\n".join(blocks)

    # ---- projects ----
    def _project_root(request: Request, pid: str | None):
        """(meta, files_root) for an owner-scoped project, or (None, None)."""
        if not pid:
            return None, None
        owner = _owner(request)
        safe_pid = os.path.basename(pid)        # no traversal in the id itself
        meta = PJ.read_meta(projects_dir, owner, safe_pid)
        if not meta:
            return None, None
        return meta, PJ.files_root(projects_dir, owner, safe_pid)

    def _augment_with_project(request: Request, message: str, pid: str | None) -> str:
        meta, root = _project_root(request, pid)
        if not meta or root is None:
            return message
        return (f"[Project: {meta['name']}]\n"
                f"You are working in this project's files directory:\n  {root}\n"
                f"Explore with fs.list / fs.read / fs.grep on paths under it, change "
                f"files with fs.write / fs.edit, and hand results back with "
                f"deliver.files if asked. Work only inside that directory.\n"
                f"Current files:\n{PJ.tree_text(root)}\n\n" + message)

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
        if not PJ.delete_project(projects_dir, _owner(request), os.path.basename(pid)):
            raise HTTPException(status_code=404, detail="no such project")
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

    @app.put("/api/projects/{pid}/file")
    async def project_write_file(pid: str, request: Request, path: str):
        meta, root = _project_root(request, pid)
        if not meta:
            raise HTTPException(status_code=404, detail="no such project")
        body = await request.body()
        if len(body) > max_project_file_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail="file too large")
        out = PJ.write_file(root, path, body)
        if out is None:
            raise HTTPException(status_code=400, detail="invalid path")
        return out

    @app.post("/api/projects/{pid}/upload")
    async def project_upload(pid: str, request: Request, path: str):
        meta, root = _project_root(request, pid)
        if not meta:
            raise HTTPException(status_code=404, detail="no such project")
        body = await request.body()
        if len(body) > max_project_file_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail="file too large")
        # keep any subdir, sanitise the filename component
        rel = "/".join(_safe_name(seg) for seg in path.replace("\\", "/").split("/") if seg)
        out = PJ.write_file(root, rel, body)
        if out is None:
            raise HTTPException(status_code=400, detail="invalid path")
        return out

    @app.delete("/api/projects/{pid}/file")
    async def project_delete_file(pid: str, request: Request, path: str):
        meta, root = _project_root(request, pid)
        if not meta:
            raise HTTPException(status_code=404, detail="no such project")
        if not PJ.delete_path(root, path):
            raise HTTPException(status_code=404, detail="no such file")
        return {"ok": True, "deleted": path}

    # ---- chat / stream ----
    @app.post("/api/chat")
    async def chat(req: ChatRequest, request: Request):
        run_id = uuid.uuid4().hex
        u = _user(request)
        # Opportunistic, throttled cleanup of expired unsaved outputs.
        if time.time() - _sweep_state["last"] > 600:
            _sweep_state["last"] = time.time()
            try:
                sweep(outputs_dir, output_ttl_hours)
            except Exception:
                pass
        disabled = set() if u["username"] == "_token" else set(
            users.get_disabled_tools(u["username"]))
        all_names = [t.name for t in runtime.registry.all()]
        enabled = [n for n in all_names if n not in disabled]
        allow = [n for n in req.tools if n in enabled] if req.tools is not None else enabled

        async def on_event(event: dict) -> None:
            await bus.publish(run_id, event)

        message = _augment_with_attachments(request, req.message, req.attachments)
        message = _augment_with_project(request, message, req.project_id)
        coro = runtime.run(
            message,
            share_private=req.share_private,
            auto_confirm=req.auto_confirm,
            think=req.think,
            tools=allow,
            budget_overrides=req.budget_overrides,
            run_id=run_id,
            on_event=on_event,
            confirm_provider=provider,
            history=req.history,
            owner=_owner(request),
            stream=True,
        )
        task = asyncio.create_task(coro)
        tasks[run_id] = task
        run_owner[run_id] = _owner(request)

        def _cleanup(_t: asyncio.Task) -> None:
            async def forget_later():
                await asyncio.sleep(_FORGET_AFTER_S)
                bus.forget(run_id)
                tasks.pop(run_id, None)
                run_owner.pop(run_id, None)
            asyncio.create_task(forget_later())
        task.add_done_callback(_cleanup)
        return {"run_id": run_id}

    @app.get("/api/stream/{run_id}")
    async def stream(run_id: str, request: Request,
                     last_event_id: str | None = Header(default=None)):
        if not _can_access_run(request, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        after = int(last_event_id) if (last_event_id or "").isdigit() else 0
        q = bus.subscribe(run_id, after_seq=after)
        from sse_starlette.sse import EventSourceResponse

        async def gen():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield {"event": "ping", "data": "{}"}
                        continue
                    yield {"id": str(event.get("seq", 0)),
                           "event": event["type"],
                           "data": json.dumps(event)}
                    if event["type"] in ("run_finish",):
                        break
            finally:
                bus.unsubscribe(run_id, q)

        return EventSourceResponse(gen())

    @app.post("/api/approve/{run_id}")
    async def approve(run_id: str, req: ApproveRequest, request: Request):
        if not _can_access_run(request, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        ok = provider.resolve(run_id, req.confirmation_id, req.approved)
        if not ok:
            raise HTTPException(status_code=404, detail="no pending confirmation")
        return {"ok": True}

    @app.post("/api/cancel/{run_id}")
    async def cancel(run_id: str, request: Request):
        if not _can_access_run(request, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        t = tasks.get(run_id)
        if t and not t.done():
            t.cancel()
            return {"ok": True, "cancelled": True}
        return {"ok": True, "cancelled": False}

    @app.get("/api/health")
    async def health():
        return {"ok": True, "tools": len(runtime.registry.all())}

    # ---- tools (per-user enable/disable) ----
    @app.get("/api/tools")
    async def list_tools(request: Request):
        u = _user(request)
        disabled = set() if u["username"] == "_token" else set(
            users.get_disabled_tools(u["username"]))
        out = []
        for t in sorted(runtime.registry.all(), key=lambda x: x.name):
            out.append({
                "name": t.name,
                "namespace": t.name.split(".")[0],
                "description": getattr(t, "description", "") or "",
                "private": bool(getattr(t, "private", False)),
                "requires_confirmation": bool(getattr(t, "requires_confirmation", False)),
                "enabled": t.name not in disabled,
            })
        return {"tools": out}

    @app.post("/api/tools")
    async def save_tools(req: ToolsRequest, request: Request):
        u = _user(request)
        if u["username"] != "_token":
            users.set_disabled_tools(u["username"], req.disabled)
        return {"ok": True, "disabled": sorted(set(req.disabled))}

    # ---- saved chats (per user) ----
    @app.get("/api/chats")
    async def list_chats(request: Request):
        return {"chats": chats.list(owner=_owner(request))}

    @app.post("/api/chats")
    async def save_chat(req: SaveChatRequest, request: Request):
        result = chats.upsert(req.id, req.title, [t.model_dump() for t in req.turns],
                              owner=_owner(request), project_id=req.project_id)
        # Saving the chat keeps its runs' delivered files (otherwise swept).
        for t in req.turns:
            if t.run_id:
                mark_saved(outputs_dir, t.run_id, True)
        return result

    @app.get("/api/chats/{chat_id}")
    async def get_chat(chat_id: str, request: Request):
        c = chats.get(chat_id, owner=_owner(request))
        if not c:
            raise HTTPException(status_code=404, detail="no such chat")
        return c

    @app.patch("/api/chats/{chat_id}")
    async def rename_chat(chat_id: str, req: RenameRequest, request: Request):
        if not chats.rename(chat_id, req.title, owner=_owner(request)):
            raise HTTPException(status_code=404, detail="no such chat")
        return {"ok": True}

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: str, request: Request):
        owner = _owner(request)
        existing = chats.get(chat_id, owner=owner)   # read run_ids before delete
        if not chats.delete(chat_id, owner=owner):
            raise HTTPException(status_code=404, detail="no such chat")
        for t in (existing or {}).get("turns", []):
            if t.get("run_id"):
                delete_output(outputs_dir, t["run_id"])
        return {"ok": True, "deleted": chat_id}

    # ============================ ADMIN ============================
    @app.get("/api/admin/prompt")
    async def get_prompt():
        path = runtime.config_path.parent.parent / runtime.config["orchestrator"]["system_prompt"]
        return {"content": runtime.system_prompt, "path": str(path)}

    @app.put("/api/admin/prompt")
    async def put_prompt(req: PromptRequest):
        path = runtime.config_path.parent.parent / runtime.config["orchestrator"]["system_prompt"]
        path.write_text(req.content)
        runtime.system_prompt = req.content
        return {"ok": True, "bytes": len(req.content)}

    @app.get("/api/admin/status")
    async def admin_status():
        async def probe(url: str) -> dict:
            t0 = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=4) as c:
                    r = await c.get(url)
                return {"ok": r.status_code < 500, "status": r.status_code,
                        "latency_ms": int((time.monotonic() - t0) * 1000)}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        services = [{"name": "LiteLLM proxy", "url": runtime.litellm_base,
                     **await probe(runtime.litellm_base + "/health")}]
        for s in (web_cfg.get("services") or []):
            services.append({"name": s.get("name", s.get("url")), "url": s.get("url"),
                             **await probe(s["url"])})

        storage = []
        for name, p in [("trace", runtime.config["trace"]["db_path"]),
                        ("chats", chats.db_path), ("users", users.db_path),
                        ("memory", (runtime.config.get("tools", {}).get("memory", {}) or {}).get("db_path")),
                        ("rag", (runtime.config.get("tools", {}).get("rag", {}) or {}).get("db_path"))]:
            if p and Path(p).exists():
                storage.append({"name": name, "path": str(p),
                                "size_bytes": Path(p).stat().st_size})

        return {
            "process": {
                "uptime_s": int(time.time() - started_at),
                "active_runs": sum(1 for t in tasks.values() if not t.done()),
                "tools": len(runtime.registry.all()),
                "model": runtime.model,
                "users": users.count(),
            },
            "services": services,
            "models": sorted((runtime.cost_table or {}).keys()),
            "storage": storage,
        }

    @app.get("/api/admin/logs")
    async def admin_logs(limit: int = 50, run_id: str | None = None):
        db = runtime.config["trace"]["db_path"]
        if not Path(db).exists():
            return {"runs": [], "events": []}
        conn = sqlite3.connect(db, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            if run_id:
                rows = conn.execute(
                    "SELECT ts, kind, iteration, payload_json FROM events "
                    "WHERE run_id=? ORDER BY id LIMIT 500", (run_id,)).fetchall()
                return {"events": [dict(r) for r in rows]}
            rows = conn.execute(
                "SELECT id, started_at, finished_at, status, error, "
                "substr(user_message,1,160) AS message FROM runs "
                "ORDER BY started_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
            runs = []
            for r in rows:
                d = dict(r)
                if d.get("finished_at") and d.get("started_at"):
                    d["duration_s"] = round(d["finished_at"] - d["started_at"], 2)
                runs.append(d)
            return {"runs": runs}
        finally:
            conn.close()

    @app.get("/api/admin/users")
    async def admin_users():
        return {"users": users.list()}

    @app.post("/api/admin/users")
    async def admin_create_user(req: NewUserRequest):
        if users.get(req.username):
            raise HTTPException(status_code=409, detail="user exists")
        if not req.username or not req.password:
            raise HTTPException(status_code=400, detail="username and password required")
        return users.create(req.username, req.password, is_admin=req.is_admin)

    @app.post("/api/admin/users/{username}/password")
    async def admin_set_password(username: str, req: PasswordRequest):
        if not users.set_password(username, req.password):
            raise HTTPException(status_code=404, detail="no such user")
        return {"ok": True}

    @app.post("/api/admin/users/{username}/admin")
    async def admin_set_admin(username: str, req: AdminFlagRequest):
        cur = users.get(username)
        if not req.is_admin and cur and cur["is_admin"] and users.admin_count() <= 1:
            raise HTTPException(status_code=400, detail="cannot demote the last admin")
        if not users.set_admin(username, req.is_admin):
            raise HTTPException(status_code=404, detail="no such user")
        return {"ok": True}

    @app.post("/api/admin/users/{username}/2fa/reset")
    async def admin_reset_2fa(username: str):
        if not users.get(username):
            raise HTTPException(status_code=404, detail="no such user")
        users.disable_totp(username)  # clears secret + backup codes
        return {"ok": True}

    @app.delete("/api/admin/users/{username}")
    async def admin_delete_user(username: str, request: Request):
        if username == _user(request)["username"]:
            raise HTTPException(status_code=400, detail="cannot delete yourself")
        u = users.get(username)
        if u and u["is_admin"] and users.admin_count() <= 1:
            raise HTTPException(status_code=400, detail="cannot delete the last admin")
        if not users.delete(username):
            raise HTTPException(status_code=404, detail="no such user")
        return {"ok": True, "deleted": username}

    # ---- admin: RAG management ----
    def _rag_db() -> str:
        return ((runtime.config.get("tools", {}) or {}).get("rag", {}) or {}).get(
            "db_path", "/srv/orchestrator/data/rag.db")

    def _rag_conn() -> sqlite3.Connection | None:
        path = _rag_db()
        if not Path(path).exists():
            return None
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @app.get("/api/admin/rag")
    async def admin_rag():
        path = _rag_db()
        conn = _rag_conn()
        if conn is None:
            return {"collections": [], "total_chunks": 0, "db_bytes": 0,
                    "db_path": path}
        try:
            try:
                rows = conn.execute(
                    "SELECT collection, COUNT(*) AS chunks, "
                    "COUNT(DISTINCT source) AS sources, "
                    "SUM(LENGTH(text)) AS text_bytes, "
                    "SUM(LENGTH(embedding)) AS embedding_bytes "
                    "FROM rag_doc GROUP BY collection ORDER BY collection").fetchall()
                total = conn.execute("SELECT COUNT(*) AS c FROM rag_doc").fetchone()["c"]
            except sqlite3.OperationalError:
                rows, total = [], 0   # table not created yet
            return {"collections": [dict(r) for r in rows], "total_chunks": total,
                    "db_bytes": Path(path).stat().st_size, "db_path": path}
        finally:
            conn.close()

    @app.get("/api/admin/rag/{collection}")
    async def admin_rag_collection(collection: str):
        conn = _rag_conn()
        if conn is None:
            return {"collection": collection, "sources": []}
        try:
            try:
                rows = conn.execute(
                    "SELECT source, COUNT(*) AS chunks FROM rag_doc "
                    "WHERE collection=? GROUP BY source ORDER BY source",
                    (collection,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            return {"collection": collection, "sources": [dict(r) for r in rows]}
        finally:
            conn.close()

    @app.delete("/api/admin/rag/{collection}")
    async def admin_rag_delete(collection: str, source: str | None = None):
        conn = _rag_conn()
        if conn is None:
            return {"deleted": 0, "collection": collection}
        try:
            if source is not None:
                cur = conn.execute(
                    "DELETE FROM rag_doc WHERE collection=? AND source=?",
                    (collection, source))
            else:
                cur = conn.execute("DELETE FROM rag_doc WHERE collection=?",
                                   (collection,))
            conn.commit()
            return {"deleted": cur.rowcount, "collection": collection,
                    "source": source}
        except sqlite3.OperationalError:
            return {"deleted": 0, "collection": collection}
        finally:
            conn.close()

    @app.post("/api/admin/rag/empty")
    async def admin_rag_empty():
        conn = _rag_conn()
        if conn is None:
            return {"deleted": 0, "vacuumed": False}
        try:
            try:
                cur = conn.execute("DELETE FROM rag_doc")
                conn.commit()
                deleted = cur.rowcount
                conn.execute("VACUUM")
                return {"deleted": deleted, "vacuumed": True}
            except sqlite3.OperationalError:
                return {"deleted": 0, "vacuumed": False}
        finally:
            conn.close()

    return app


_CONFIG = os.environ.get("ORCH_CONFIG", "/srv/orchestrator/config/runtime.yaml")
app = create_app(_CONFIG) if Path(_CONFIG).exists() else None
