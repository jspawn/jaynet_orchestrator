"""FastAPI + SSE service: chat with the orchestrator and watch the loop live.

Phase 9 + auth/admin:
- The AgentRuntime is created once and lives for the process.
- POST /api/chat starts a run as a background task and returns its run_id; the run
  streams transport-neutral events into an EventBus; GET /api/stream/{run_id} is
  the SSE feed. Approvals/cancels flow back over plain POSTs.
- Access is gated by login: HMAC-signed session cookies (see web/auth.py), with an
  optional JAYNET_WEB_TOKEN bearer for API/CLI. Per-user tool toggles persist in the
  user store and are applied to each run. Admins get /admin: edit the system
  prompt, see service status, and read run logs/users.

Nothing here leaks into runtime/loop.py — the loop only knows `on_event` and
`confirm_provider`.

Routes live in web/routes_*.py; create_app builds the shared state (stores,
dirs, helper closures) and registers them in the original section order.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from runtime.confirm import WebConfirmationProvider, WebQuestionProvider
from runtime.events import EventBus
from runtime import serving as S
from runtime import eval_runner
from runtime.jaypack import _MAX_BYTES as _PACK_MAX_BYTES
from runtime.loop import AgentRuntime
from runtime.outputs import (is_safe_run_id, mark_saved, sweep, sweep_scratch)
from tools.model.catalog import ModelList, ModelUse, _served_matches
from web.auth import LoginThrottle, UserStore, read_session, resolve_secret
from web.ctx import (_BUDGET_KEYS, _COOKIE, _MAX_INLINE_CHARS, BodyTooLarge,
                     _classify, _safe_name)
from web.store import ChatStore, FlagStore, ReportStore
from web import projects as PJ
from web import routes_admin, routes_chats, routes_pages, routes_procs
from web import routes_eval, routes_projects, routes_run, routes_studio
from web import routes_uploads
from runtime.env import env

# NOTE: ModelList, ModelUse, _litellm_model_ids, _imp_local_alive,
# _parse_llama_metrics and _FORGET_AFTER_S stay here even though the code using
# them moved to web/routes_*.py — those modules look them up on THIS module at
# call time, and tests monkeypatch them here.

_FORGET_AFTER_S = 120
_OPEN_PATHS = {"/login", "/api/login", "/api/health", "/favicon.ico"}

# Global request-body ceiling (memory-DoS guard): the upload-ish routes carry
# their own larger per-route caps (see _body_limit in create_app); every other
# body — including the open /api/login — shares this one. Bodies are counted
# as they stream in, so chunked requests (no Content-Length) are capped too.
# The cap is on the request direction only — SSE/GET streams are unaffected.
_MAX_BODY_BYTES = 4 * 1024 * 1024


class _BodyCapMiddleware:
    """Pure-ASGI middleware: 413 when a request body exceeds its route's cap.

    Small (JSON-ish) bodies are buffered with a running counter and replayed
    downstream — the app always sees either a complete body or nothing at
    all. Large upload bodies can't be buffered (that would be the DoS this
    guards against), so they stream through with a running counter; tripping
    the cap raises BodyTooLarge out of the receive channel, caught below —
    with `except*` because starlette's BaseHTTPMiddleware runs the body read
    inside an anyio task group, which re-raises it as an ExceptionGroup."""

    def __init__(self, app, limit_for):
        self.app = app
        self.limit_for = limit_for

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope["path"])
        cl = dict(scope["headers"]).get(b"content-length", b"").decode()
        if cl.isdigit() and int(cl) > limit:
            await self._too_large(send)
            return
        if limit <= _MAX_BODY_BYTES:
            await self._capped_buffer(scope, receive, send, limit)
            return
        seen = 0

        async def capped_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise BodyTooLarge
            return message

        try:
            await self.app(scope, capped_receive, send)
        except* BodyTooLarge:
            # The body read aborted before any response was started.
            await self._too_large(send)

    async def _capped_buffer(self, scope, receive, send, limit):
        parts: list[bytes] = []
        seen = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return                      # client gave up mid-body
            seen += len(message.get("body", b""))
            if seen > limit:
                await self._too_large(send)
                return
            parts.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(parts)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _too_large(send):
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body",
                    "body": b'{"detail":"request body too large"}'})


def _parse_llama_metrics(text: str) -> dict:
    """Parse llama-server's Prometheus /metrics text into {name: float}.
    Only plain `llamacpp:name value` lines are kept (comments/metadata dropped)."""
    out: dict[str, float] = {}
    for line in (text or "").splitlines():
        if not line.startswith("llamacpp:"):
            continue
        k, _, v = line.partition(" ")
        try:
            out[k[len("llamacpp:"):]] = float(v)
        except ValueError:
            continue
    return out


async def _litellm_model_ids(runtime) -> set | None:
    """Aliases the LiteLLM proxy currently serves; None when unreachable."""
    key = os.environ.get("LITELLM_MASTER_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=2.5) as c:
            r = await c.get(runtime.litellm_base + "/v1/models",
                            headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 200:
                return {m.get("id") for m in (r.json().get("data") or [])
                        if m.get("id")}
    except Exception:
        pass
    return None


async def _imp_local_alive(runtime, imp: dict) -> bool:
    """Dead-slot check for a local impersonation: is the preset's own model
    still the one serving on its fixed port? Anyone's model.use can swap the
    GPU-1 slot out from under an active override."""
    p = ((runtime.config.get("models") or {}).get("presets") or {}).get(
        imp.get("preset") or "") or {}
    port = p.get("port")
    if not port:
        return False
    host = (p.get("remote_host") or "").strip() or "127.0.0.1"
    try:
        mid = await S.query_model_id(f"http://{host}:{int(port)}")
    except Exception:
        return False
    return bool(mid) and _served_matches(mid, p)


def create_app(config_path: str | None = None) -> FastAPI:
    from runtime.paths import CONFIG, CHATS_DB
    config_path = config_path or str(CONFIG)
    app = FastAPI(title="JayNet Orchestrator")
    runtime = AgentRuntime(config_path)
    eval_runner.set_runtime(runtime)     # lets eval.* tools reach the live loop
    bus = EventBus()
    pending: dict[tuple[str, str], asyncio.Future] = {}
    conf_cfg = runtime.config.get("confirmation", {}) or {}
    provider = WebConfirmationProvider(
        pending,
        timeout_s=float(conf_cfg.get("web_timeout_s", 300)),
        on_timeout=(conf_cfg.get("web_on_timeout", "deny") == "allow"),
    )
    pending_q: dict[tuple[str, str], asyncio.Future] = {}
    qprovider = WebQuestionProvider(
        pending_q,
        timeout_s=float(conf_cfg.get("question_timeout_s", 600)),
    )
    tasks: dict[str, asyncio.Task] = {}
    run_owner: dict[str, str | None] = {}   # run_id -> owner, for access checks
    throttle = LoginThrottle()
    token = env("ORCH_WEB_TOKEN")
    web_cfg = runtime.config.get("web", {}) or {}
    data_dir = Path(web_cfg.get("chats_db", str(CHATS_DB))).parent
    chats = ChatStore(web_cfg.get("chats_db", str(CHATS_DB)))
    flags = FlagStore(web_cfg.get("chats_db", str(CHATS_DB)))   # same file, own table
    reports = ReportStore(web_cfg.get("chats_db", str(CHATS_DB)))   # watchdog, own table
    users = UserStore(web_cfg.get("users_db", str(data_dir / "users.db")))

    # Apply any admin-persisted config overrides on top of the YAML defaults.
    _overrides = users.get_config_overrides()
    if _overrides:
        def _apply_override(cfg, dotpath, value):
            parts = dotpath.split(".")
            d = cfg
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = value
        for dp, val in _overrides.items():
            _apply_override(runtime.config, dp, val)

    # Layer the DB preset catalog over the YAML seed (admin-editable; seeds
    # itself from runtime.yaml on first use, fail-safe — see preset_store).
    from runtime.preset_store import load_into_config
    load_into_config(runtime.config)

    secret = resolve_secret(data_dir)
    cookie_secure = bool(web_cfg.get("cookie_secure", False))
    uploads_dir = Path(web_cfg.get("uploads_dir", str(data_dir / "uploads")))
    max_upload_mb = int(web_cfg.get("max_upload_mb", 25))
    max_restore_mb = int(web_cfg.get("max_restore_mb", 1024))
    outputs_dir = Path(web_cfg.get("outputs_dir", str(data_dir / "outputs")))
    output_ttl_hours = float(web_cfg.get("output_ttl_hours", 24))
    max_output_mb = int(web_cfg.get("max_output_mb", 200))
    projects_dir = Path(web_cfg.get("projects_dir", str(data_dir / "projects")))
    projects_dir.mkdir(parents=True, exist_ok=True)
    max_project_file_mb = int(web_cfg.get("max_project_file_mb", 25))
    # Per-chat scratch: the agent's work_root when NO project is active. Owner +
    # conversation scoped, separate from per-run deliverables (outputs/). This is
    # the structural workspace the fs/code tools are confined to for a bare chat.
    chat_scratch_dir = Path(web_cfg.get("chat_scratch_dir", str(data_dir / "chat-scratch")))
    chat_scratch_ttl_hours = float(web_cfg.get("chat_scratch_ttl_hours", 336))  # 14 days
    # Global (owner-scoped) wiki root: LLM-maintained knowledge not tied to one
    # project (see skills/wiki + the /llmwiki command). A PROJECT's wiki lives
    # inside its files dir instead and is deleted with the project.
    wiki_dir = Path(web_cfg.get("wiki_dir", str(data_dir / "wiki")))

    def _scratch_root(owner: str | None, cid: str | None, create: bool = True) -> Path | None:
        """The per-chat scratch files root, owner-scoped. None if no conversation id."""
        safe = _safe_name(cid or "")[:64]
        if not cid or not safe:
            return None
        root = chat_scratch_dir / (owner or "_token") / safe / "files"
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root

    def _wiki_root(owner: str | None, project_id: str | None, create: bool = True) -> Path | None:
        """The wiki dir a /llmwiki run works in: `<project>/files/wiki` when a
        project is active (so it dies with the project), else the owner's
        global wiki. None if the named project no longer exists."""
        if project_id:
            base = PJ.files_root(projects_dir, owner, os.path.basename(project_id))
            if base is None:
                return None
            root = base / "wiki"
        else:
            root = wiki_dir / (owner or "_token")
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root

    # --- admin-set global budget defaults -----------------------------------
    # Override the runtime.yaml seed and persist so they survive restarts. These
    # become the base ceilings for every run (web/token/voice/sub-agent base);
    # per-run overrides and per-user account defaults still layer on top.
    _BUDGET_CAPS = {"max_iterations": 1000, "max_wall_clock_s": 86400,
                    "max_cost_usd": 1000.0, "max_total_tokens": 100_000_000}
    budget_defaults_path = data_dir / "budget-defaults.json"

    def _coerce_budget(d: dict, allow_zero: bool = False) -> dict:
        # allow_zero (admin budget editor + its boot reload): keep explicit 0s —
        # 0 = "no ceiling" for every budget key (runtime/budget.py guards all
        # four), it is the editor's "off" state. Request-level overrides stay
        # tighten-only: a 0 there means "no opinion" and is dropped.
        out: dict = {}
        for k in _BUDGET_KEYS:
            v = d.get(k)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v < 0 or (v == 0 and not allow_zero):
                continue
            v = min(v, _BUDGET_CAPS[k])
            out[k] = int(v) if k in ("max_iterations", "max_total_tokens") else v
        return out

    try:
        if budget_defaults_path.exists():
            runtime.config["budgets"].update(
                _coerce_budget(json.loads(budget_defaults_path.read_text()),
                               allow_zero=True))
    except Exception:
        pass
    try:
        sweep(outputs_dir, output_ttl_hours)   # clean orphans/expired on boot
        sweep_scratch(chat_scratch_dir, chat_scratch_ttl_hours)
        from runtime import hf_pull
        removed = hf_pull.clean_stale_parts()   # aborted-download residue
        if removed:
            print(f"[startup] removed {removed} stale .part file(s) from "
                  "the models dir", file=sys.stderr)
    except Exception:
        pass

    app.state.runtime = runtime
    app.state.bus = bus
    app.state.pending = pending
    app.state.tasks = tasks
    app.state.reports = reports
    app.state.provider = provider
    app.state.chats = chats
    app.state.flags = flags
    app.state.users = users

    def _user(request: Request) -> dict:
        # Fail CLOSED: the middleware always sets request.state.user on non-open
        # paths, so a missing identity is a bug — never default to admin.
        return getattr(request.state, "user", None) or {"username": "_unknown", "is_admin": False}

    app.state._user = _user

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
                if getattr(runtime, "vision_enabled", False):
                    blocks.append(f"- {name} (image, {size} B) saved at {p}\n"
                                  "  This image is attached to your message — you can "
                                  "see it directly. You can also read it from the path "
                                  "with an image tool if needed.")
                else:
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

    def _image_urls_for(request: Request,
                        attachment_ids: list[str] | None) -> list[str]:
        """Base64 data URLs for image attachments, for forwarding to a vision
        brain. Only meaningful when runtime.vision_enabled; callers gate on it."""
        if not attachment_ids:
            return []
        import base64
        ext_mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}
        urls: list[str] = []
        for att_id in attachment_ids:
            p = _resolve_attachment(request, att_id)
            if not p or _classify(p.name) != "image":
                continue
            ext = p.suffix.lower().lstrip(".")
            mime = ext_mime.get(ext, "image/png")
            try:
                b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            except Exception:
                continue
            urls.append(f"data:{mime};base64,{b64}")
        return urls

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
                f"Your fs.* and code.* tools are already rooted in this project's "
                f"files — write paths relative to it. Current files:\n"
                f"{PJ.tree_text(root)}\n\n" + message)

    # ---- promote a chat/session into a project ----
    def _sweep_outputs_into_project(owner: str | None, run_ids: list, pid: str) -> int:
        """Copy every run's delivered files into the project's files root and mark
        those outputs saved (so the sweeper leaves them). Collision-safe. Returns
        the number of top-level entries copied in."""
        root = PJ.files_root(projects_dir, owner, pid)
        if root is None:
            return 0
        moved = 0
        for rid in run_ids:
            if not is_safe_run_id(rid):   # skip forged/legacy traversal ids
                continue
            src = outputs_dir / rid / "files"
            if not src.is_dir():
                continue
            for item in sorted(src.iterdir()):
                dest = root / item.name
                if dest.exists():
                    stem, ext = os.path.splitext(item.name)
                    i = 2
                    while (root / f"{stem}-{i}{ext}").exists():
                        i += 1
                    dest = root / f"{stem}-{i}{ext}"
                try:
                    if item.is_dir():
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)
                    moved += 1
                except OSError:
                    pass
            mark_saved(outputs_dir, rid, True, owner=owner)
        return moved

    def _promote_chat_to_project(owner: str | None, chat: dict, name: str) -> dict:
        """Create a project from a chat: mint the project, sweep the chat's run
        files into it, and re-save the chat bound to the new project."""
        meta = PJ.create_project(projects_dir, owner, name)
        turns = chat.get("turns", [])
        _sweep_outputs_into_project(owner, [t.get("run_id") for t in turns], meta["id"])
        chats.upsert(chat.get("id"), chat.get("title"), turns,
                     owner=owner, project_id=meta["id"])
        return meta

    # Everything the route modules' closures captured in the old create_app
    # body: stores, dirs, runtime, bus, and the shared helper closures above.
    state = SimpleNamespace(
        runtime=runtime, bus=bus, provider=provider, qprovider=qprovider,
        tasks=tasks, run_owner=run_owner, throttle=throttle, web_cfg=web_cfg,
        data_dir=data_dir, chats=chats, flags=flags, reports=reports,
        users=users, secret=secret, cookie_secure=cookie_secure,
        uploads_dir=uploads_dir, max_upload_mb=max_upload_mb,
        max_restore_mb=max_restore_mb,
        outputs_dir=outputs_dir, output_ttl_hours=output_ttl_hours,
        max_output_mb=max_output_mb, projects_dir=projects_dir,
        max_project_file_mb=max_project_file_mb,
        chat_scratch_dir=chat_scratch_dir,
        chat_scratch_ttl_hours=chat_scratch_ttl_hours,
        wiki_dir=wiki_dir,
        budget_defaults_path=budget_defaults_path,
        _scratch_root=_scratch_root, _wiki_root=_wiki_root,
        _coerce_budget=_coerce_budget,
        _user=_user, _owner=_owner, _owner_dir=_owner_dir,
        _resolve_attachment=_resolve_attachment, _can_access_run=_can_access_run,
        _augment_with_attachments=_augment_with_attachments,
        _image_urls_for=_image_urls_for,
        _project_root=_project_root, _augment_with_project=_augment_with_project,
        _sweep_outputs_into_project=_sweep_outputs_into_project,
        _promote_chat_to_project=_promote_chat_to_project,
    )

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        path = request.url.path
        if path in _OPEN_PATHS or path.startswith("/static"):
            return await call_next(request)
        sess = read_session(request.cookies.get(_COOKIE), secret)
        user = None
        if sess:
            username, epoch = sess
            u = users.get(username)
            # Reject cookies issued before a "log out everywhere" / password change.
            if u and users.session_epoch(username) == epoch:
                user = u
        if user is None and request.headers.get("authorization", "").startswith("Bearer "):
            bearer = request.headers["authorization"][7:].strip()
            if token and hmac.compare_digest(bearer.encode(), token.encode()):
                user = {"username": "_token", "is_admin": True}   # global admin token
            else:
                uname = users.verify_api_token(bearer)            # per-user API token
                if uname:
                    u = users.get(uname)
                    if u:
                        user = u
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

    # Body caps per route: the upload-ish endpoints get their own (larger)
    # ceilings, everything else shares _MAX_BODY_BYTES. Multipart routes get
    # +1 MiB of framing slack on top of the file-part cap the endpoint
    # enforces while decoding. Registered after auth_mw so it runs outermost.
    _max_upload = max_upload_mb * 1024 * 1024
    _max_project = max_project_file_mb * 1024 * 1024
    _max_restore = max_restore_mb * 1024 * 1024

    def _body_limit(path: str) -> int:
        if path == "/api/upload":
            return _max_upload
        if path == "/api/admin/restore":
            return _max_restore + 1024 * 1024
        if path == "/api/admin/studio/import":
            return _PACK_MAX_BYTES + 1024 * 1024
        if path.endswith("/file") and \
                path.startswith(("/api/projects/", "/api/chat-scratch/")):
            return _max_project
        return _MAX_BODY_BYTES

    app.add_middleware(_BodyCapMiddleware, limit_for=_body_limit)

    # Route modules, registered in the original create_app section order.
    routes_pages.register(app, state)        # pages, auth API, 2fa, account
    routes_uploads.register(app, state)      # uploads + run-output downloads
    routes_projects.register(app, state)     # projects + per-chat scratch
    routes_run.register(app, state)          # quick-reply, slash, launcher, /goal, chat/stream, voice, tools
    routes_chats.register(app, state)        # saved chats, current chat, flags
    routes_admin.register(app, state)        # /api/admin/*
    routes_studio.register(app, state)       # /api/admin/studio/*
    routes_eval.register(app, state)         # /api/admin/evals/* (+ flag make-test)
    routes_procs.register(app, state)        # managed processes, scheduler, startup hooks

    return app


_CONFIG = env("ORCH_CONFIG") or str(__import__("runtime.paths", fromlist=["CONFIG"]).CONFIG)
app = create_app(_CONFIG) if Path(_CONFIG).exists() else None
