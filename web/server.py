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
import hmac
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from runtime.confirm import WebConfirmationProvider, WebQuestionProvider
from runtime.events import EventBus
from runtime.loop import AgentRuntime
from runtime.serve_preset import parse_preset
from runtime.outputs import (deliverable_path, delete_output, is_safe_run_id,
                             mark_saved, read_manifest, sweep, sweep_scratch)
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

# Media types for inline preview (open-in-tab). Text-like types are served as
# text/plain so the browser shows them rather than downloading; html stays html.
_PREVIEW_MEDIA = {
    ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp", ".ico": "image/x-icon",
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
    ".markdown": "text/plain; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".csv": "text/plain; charset=utf-8", ".tsv": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8", ".xml": "text/plain; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8", ".yml": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8", ".js": "text/plain; charset=utf-8",
    ".css": "text/plain; charset=utf-8",
}


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
    grill: bool = False                      # clarify-first: ask when the request is unclear
    tools: list[str] | None = None
    budget_overrides: dict | None = None
    compaction: dict | None = None           # per-run context compaction override
    parallel_tools: dict | None = None       # per-run parallel-execution override
    sampling: dict | None = None             # per-run sampler override (temperature, top_p, …)
    sub_budget: dict | None = None           # per-run sub-agent (agent.spawn) budget override
    architect_threshold: int | None = None    # complexity gate (1-10); 0/None uses config default
    history: list[dict] | None = None
    attachments: list[str] | None = None   # uploaded file ids (owner-scoped)
    project_id: str | None = None           # work inside this project's files
    conversation_id: str | None = None       # stable chat id -> per-chat scratch work_root


class ApproveRequest(BaseModel):
    confirmation_id: str
    approved: bool


class AnswerRequest(BaseModel):
    ask_id: str
    answers: dict   # {qid: {value: str|list, text: str}}


# Run ids are minted server-side as uuid4 hex; a saved-chat turn carrying
# anything else is forged, and rejecting it here keeps the id from ever
# reaching the filesystem (outputs/<run_id>/).
_MINTED_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")

# Usernames become path components (uploads/projects/chat-scratch owner dirs),
# so admin-created names must be a single, boring, traversal-free component.
_USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class TurnModel(BaseModel):
    user_message: str
    answer: str = ""
    run_id: str | None = None
    status: str | None = None
    events: list[dict] | None = None

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, v: str | None) -> str | None:
        if v is not None and not _MINTED_RUN_ID.match(v):
            raise ValueError("run_id must be a server-minted id")
        return v


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


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class BudgetDefaultsRequest(BaseModel):
    max_iterations: int | None = None
    max_wall_clock_s: int | None = None
    max_cost_usd: float | None = None
    max_total_tokens: int | None = None


class ApiTokenRequest(BaseModel):
    name: str = ""


class VoiceRequest(BaseModel):
    text: str
    conversation_id: str | None = None
    stream: bool = False


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


def create_app(config_path: str | None = None) -> FastAPI:
    from runtime.paths import CONFIG, CHATS_DB, RAG_DB, DATA
    config_path = config_path or str(CONFIG)
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
    pending_q: dict[tuple[str, str], asyncio.Future] = {}
    qprovider = WebQuestionProvider(
        pending_q,
        timeout_s=float(conf_cfg.get("question_timeout_s", 600)),
    )
    tasks: dict[str, asyncio.Task] = {}
    run_owner: dict[str, str | None] = {}   # run_id -> owner, for access checks
    throttle = LoginThrottle()
    token = os.environ.get("ORCH_WEB_TOKEN")
    web_cfg = runtime.config.get("web", {}) or {}
    data_dir = Path(web_cfg.get("chats_db", str(CHATS_DB))).parent
    chats = ChatStore(web_cfg.get("chats_db", str(CHATS_DB)))
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
    # Per-chat scratch: the agent's work_root when NO project is active. Owner +
    # conversation scoped, separate from per-run deliverables (outputs/). This is
    # the structural workspace the fs/code tools are confined to for a bare chat.
    chat_scratch_dir = Path(web_cfg.get("chat_scratch_dir", str(data_dir / "chat-scratch")))
    chat_scratch_ttl_hours = float(web_cfg.get("chat_scratch_ttl_hours", 336))  # 14 days

    def _scratch_root(owner: str | None, cid: str | None, create: bool = True) -> Path | None:
        """The per-chat scratch files root, owner-scoped. None if no conversation id."""
        safe = _safe_name(cid or "")[:64]
        if not cid or not safe:
            return None
        root = chat_scratch_dir / (owner or "_token") / safe / "files"
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root

    # --- admin-set global budget defaults -----------------------------------
    # Override the runtime.yaml seed and persist so they survive restarts. These
    # become the base ceilings for every run (web/token/voice/sub-agent base);
    # per-run overrides and per-user account defaults still layer on top.
    _BUDGET_KEYS = ("max_iterations", "max_wall_clock_s", "max_cost_usd", "max_total_tokens")
    _BUDGET_CAPS = {"max_iterations": 1000, "max_wall_clock_s": 86400,
                    "max_cost_usd": 1000.0, "max_total_tokens": 100_000_000}
    budget_defaults_path = data_dir / "budget-defaults.json"

    def _coerce_budget(d: dict) -> dict:
        out: dict = {}
        for k in _BUDGET_KEYS:
            v = d.get(k)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v <= 0:
                continue
            v = min(v, _BUDGET_CAPS[k])
            out[k] = int(v) if k in ("max_iterations", "max_total_tokens") else v
        return out

    try:
        if budget_defaults_path.exists():
            runtime.config["budgets"].update(
                _coerce_budget(json.loads(budget_defaults_path.read_text())))
    except Exception:
        pass
    _sweep_state = {"last": 0.0}
    try:
        sweep(outputs_dir, output_ttl_hours)   # clean orphans/expired on boot
        sweep_scratch(chat_scratch_dir, chat_scratch_ttl_hours)
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

    @app.get("/account")
    async def account_page():
        return FileResponse(_STATIC / "account.html")

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
        resp.set_cookie(_COOKIE, sign_session(u["username"], secret,
                                               users.session_epoch(u["username"])),
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
        is_token = u["username"] == "_token"
        twofa = False if is_token else users.has_totp(u["username"])
        budget = {} if is_token else users.get_budget_defaults(u["username"])
        return {"username": u["username"], "is_admin": u["is_admin"],
                "twofa": twofa, "budget": budget,
                "budget_defaults": {k: runtime.config["budgets"].get(k) for k in _BUDGET_KEYS},
                "sub_iterations_default": ((runtime.config.get("agent", {}).get("default_budget") or {}).get("max_iterations")
                                           or runtime.config.get("agent", {}).get("default_sub_iterations", 8)),
                "vision": bool(getattr(runtime, "vision_enabled", False)),
                "max_file_mb": max_project_file_mb,
                "brain_model": (runtime.brain_info or {}).get("model", "")}

    @app.get("/api/models")
    async def models(request: Request):
        _user(request)  # auth-gate
        orch_cfg = runtime.config.get("orchestrator", {})
        orch_alias = runtime.model
        brain_model = (runtime.brain_info or {}).get("model", "") or orch_alias
        delegate_cfg = (runtime.config.get("tools", {}).get("code", {})
                        .get("delegate", {}) or {})
        coder_alias = delegate_cfg.get("model")
        # Best-effort liveness + underlying-model map from LiteLLM.
        available = None  # None => unknown (proxy unreachable)
        underlying = {}   # alias -> real model string (from /model/info)
        key = os.environ.get("LITELLM_MASTER_KEY", "")
        try:
            async with httpx.AsyncClient(timeout=2.5) as c:
                r = await c.get(runtime.litellm_base + "/v1/models",
                                headers={"Authorization": f"Bearer {key}"})
                if r.status_code == 200:
                    available = {m.get("id") for m in (r.json().get("data") or [])}
                try:  # underlying model names (e.g. local-coder -> openai/ornith-…)
                    ri = await c.get(runtime.litellm_base + "/model/info",
                                     headers={"Authorization": f"Bearer {key}"})
                    if ri.status_code == 200:
                        for m in (ri.json().get("data") or []):
                            name = m.get("model_name")
                            real = (m.get("litellm_params") or {}).get("model")
                            if name and real:
                                underlying[name] = real
                except Exception:
                    pass
        except Exception:
            available = None

        def online(alias):
            return None if available is None else (alias in available)

        def card(alias, preset_path, model_fallback):
            """A model's display card: real name + curated 'present settings',
            read from its llama-serve.sh preset (same source the brain uses)."""
            c = {"alias": alias, "model": model_fallback or alias,
                 "online": online(alias), "settings": {}}
            d = parse_preset(preset_path) if preset_path else {}
            if d:
                mp = d.get("MODEL_PATH", "").strip()
                if mp:
                    c["model"] = Path(mp).name
                elif d.get("ALIAS", "").strip():
                    c["model"] = d["ALIAS"].strip()
                endpoint = ":".join(x for x in (d.get("HOST", "").strip(),
                                                d.get("PORT", "").strip()) if x)
                kv = "/".join(x for x in (d.get("CACHE_TYPE_K", "").strip(),
                                          d.get("CACHE_TYPE_V", "").strip()) if x)
                c["settings"] = {k: v for k, v in {
                    "model": c["model"],
                    "context": d.get("CTX_SIZE", "").strip(),
                    "endpoint": endpoint,
                    "backend": d.get("BACKEND", "").strip(),
                    "GPU": d.get("DEVICE", "").strip() or d.get("GPU", "").strip(),
                    "KV cache": kv,
                    "vision": "yes" if d.get("MMPROJ", "").strip() else "",
                }.items() if v}
            return c

        brain_preset = os.environ.get("ORCH_BRAIN_PRESET") or orch_cfg.get("brain_preset")
        out = {"orchestrator": card(orch_alias, brain_preset, brain_model)}
        if coder_alias:
            coder_preset = (os.environ.get("ORCH_CODER_PRESET")
                            or delegate_cfg.get("preset"))
            cc = card(coder_alias, coder_preset, None)
            if cc["model"] == coder_alias and underlying.get(coder_alias):
                cc["model"] = underlying[coder_alias]   # at least the real backend model
            out["coder"] = cc
        return out

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

    # ---- account (self-service) ----
    @app.get("/api/account")
    async def account_info(request: Request):
        u = _user(request)
        if u["username"] == "_token":
            raise HTTPException(status_code=403, detail="token session has no account")
        return {"username": u["username"], "is_admin": u["is_admin"],
                "twofa": users.has_totp(u["username"]),
                "backup_remaining": users.backup_codes_remaining(u["username"])}

    @app.get("/api/account/usage")
    async def account_usage(request: Request):
        owner = _owner(request)
        empty = {"monthly": [], "yearly": [], "total": {"runs": 0, "tokens": 0, "cost": 0}}
        db = runtime.config["trace"]["db_path"]
        if owner is None or not Path(db).exists():
            return empty
        conn = sqlite3.connect(db, timeout=10)
        try:
            def agg(fmt: str, limit: int):
                # fmt is a hardcoded literal ('%Y-%m' / '%Y'); owner/limit are bound params
                rows = conn.execute(
                    f"SELECT strftime('{fmt}', started_at, 'unixepoch') AS period, "
                    "COUNT(*) AS runs, COALESCE(SUM(total_tokens),0) AS tokens, "
                    "COALESCE(SUM(cost_usd),0.0) AS cost FROM runs "
                    "WHERE owner=? GROUP BY period ORDER BY period DESC LIMIT ?",
                    (owner, limit)).fetchall()
                return [{"period": p, "runs": r, "tokens": int(t), "cost": round(c, 4)}
                        for (p, r, t, c) in rows]
            tr, tt, tc = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(SUM(cost_usd),0.0) "
                "FROM runs WHERE owner=?", (owner,)).fetchone()
            return {"monthly": agg("%Y-%m", 24), "yearly": agg("%Y", 10),
                    "total": {"runs": tr, "tokens": int(tt), "cost": round(tc, 4)}}
        finally:
            conn.close()

    @app.post("/api/account/password")
    async def account_password(req: PasswordChangeRequest, request: Request):
        u = _user(request)
        if u["username"] == "_token":
            raise HTTPException(status_code=403, detail="token session cannot change password")
        if not users.verify(u["username"], req.current_password):
            raise HTTPException(status_code=403, detail="current password is incorrect")
        if len(req.new_password) < 8:
            raise HTTPException(status_code=400, detail="new password must be at least 8 characters")
        users.set_password(u["username"], req.new_password)   # bumps session_epoch
        # Re-issue this browser's cookie at the new epoch so the password change
        # signs out other sessions but keeps the current one alive.
        resp = JSONResponse({"ok": True})
        resp.set_cookie(_COOKIE, sign_session(u["username"], secret,
                                               users.session_epoch(u["username"])),
                        httponly=True, samesite="lax", secure=cookie_secure,
                        max_age=7 * 24 * 3600, path="/")
        return resp

    @app.post("/api/account/logout-all")
    async def account_logout_all(request: Request):
        u = _user(request)
        if u["username"] == "_token":
            raise HTTPException(status_code=403, detail="token session")
        users.bump_session_epoch(u["username"])
        resp = JSONResponse({"ok": True})
        resp.set_cookie(_COOKIE, sign_session(u["username"], secret,
                                               users.session_epoch(u["username"])),
                        httponly=True, samesite="lax", secure=cookie_secure,
                        max_age=7 * 24 * 3600, path="/")
        return resp

    @app.get("/api/account/runs")
    async def account_runs(request: Request, limit: int = 50):
        owner = _owner(request)
        db = runtime.config["trace"]["db_path"]
        if owner is None or not Path(db).exists():
            return {"runs": []}
        conn = sqlite3.connect(db, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, started_at, finished_at, status, "
                "COALESCE(total_tokens,0) AS total_tokens, COALESCE(cost_usd,0) AS cost_usd, "
                "substr(user_message,1,120) AS message FROM runs "
                "WHERE owner=? ORDER BY started_at DESC LIMIT ?",
                (owner, max(1, min(limit, 200)))).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                if d.get("finished_at") and d.get("started_at"):
                    d["duration_s"] = round(d["finished_at"] - d["started_at"], 2)
                out.append(d)
            return {"runs": out}
        finally:
            conn.close()

    @app.get("/api/account/budget")
    async def account_budget_get(request: Request):
        u = _user(request)
        if u["username"] == "_token":
            return {"budget": {}}
        return {"budget": users.get_budget_defaults(u["username"])}

    @app.post("/api/account/budget")
    async def account_budget_set(req: BudgetDefaultsRequest, request: Request):
        u = _user(request)
        if u["username"] == "_token":
            raise HTTPException(status_code=403, detail="token session")
        users.set_budget_defaults(u["username"], req.model_dump())
        return {"ok": True, "budget": users.get_budget_defaults(u["username"])}

    @app.get("/api/account/tokens")
    async def account_tokens(request: Request):
        u = _user(request)
        if u["username"] == "_token":
            raise HTTPException(status_code=403, detail="token session has no account")
        return {"tokens": users.list_api_tokens(u["username"])}

    @app.post("/api/account/tokens")
    async def account_token_create(req: ApiTokenRequest, request: Request):
        u = _user(request)
        if u["username"] == "_token":
            raise HTTPException(status_code=403, detail="token session cannot mint tokens")
        # token is returned in cleartext exactly once
        return users.create_api_token(u["username"], req.name)

    @app.delete("/api/account/tokens/{token_id}")
    async def account_token_revoke(token_id: int, request: Request):
        u = _user(request)
        if u["username"] == "_token":
            raise HTTPException(status_code=403, detail="token session")
        if not users.revoke_api_token(u["username"], token_id):
            raise HTTPException(status_code=404, detail="no such token")
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

    @app.post("/api/chat-files")
    async def chat_files(req: dict, request: Request):
        """List the deliverables a chat has produced, from its turns' run_ids.
        Owner-scoped via each output's manifest (same check as /api/output), so a
        caller only ever sees its own files. Works for unsaved chats too: the
        client passes the run_ids it knows — persistence isn't required."""
        owner = _owner(request)
        run_ids = (req or {}).get("run_ids") or []
        entries, seen = [], set()
        for rid in run_ids[:500]:
            if not rid or rid in seen:
                continue
            seen.add(rid)
            m = read_manifest(outputs_dir, rid)
            if not m or m.get("owner") != owner:
                continue
            entries.append({
                "run_id": rid,
                "name": m.get("name") or rid,
                "size": m.get("size") or 0,
                "kind": m.get("kind") or "file",
                "saved": bool(m.get("saved")),
                "created_at": m.get("created_at"),
            })
        return {"entries": entries}

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
            mark_saved(outputs_dir, rid, True)
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

    # "create a project [called X]" spoken to the voice channel. Anchored so it
    # only fires on a clear imperative, not when 'project' appears mid-sentence.
    _CREATE_PROJECT_RE = re.compile(
        r"^\s*(?:jaynet[,.\s]+)?(?:please[,.\s]+)?"
        r"(?:create|make|start|set\s*up|save\s+(?:this|it|the\s+chat)\s+as)\s+"
        r"(?:a\s+|an\s+)?(?:new\s+)?project"
        r"(?:\s+(?:called|named|titled|for)\s+(?P<name>.+?))?\s*[.!]?\s*$",
        re.IGNORECASE)

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
            # guessed from the suffix, no forced attachment disposition.
            return FileResponse(str(p))
        return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")

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
            return FileResponse(str(p))
        return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")

    @app.put("/api/chat-scratch/{cid}/file")
    async def scratch_write_file(cid: str, request: Request, path: str):
        root = _scratch_root(_owner(request), cid)
        body = await request.body()
        if len(body) > max_project_file_mb * 1024 * 1024:
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

    # ---- quick-reply fast-path (greetings, thanks, bye) ----
    from runtime.quick_reply import QuickReply
    quick_reply = QuickReply()
    print(f"[quick-reply] loaded {len(quick_reply)} patterns")

    async def _fast_reply(run_id: str, text: str, owner: str | None):
        """Emit the same SSE events as a real run, but with a canned response."""
        import time as _t
        t0 = _t.time()
        seq = {"n": 0}

        async def emit(event_type: str, data: dict) -> None:
            # Same envelope as the loop's emit — seq in particular, so a client
            # connecting after publish still replays these from the buffer.
            seq["n"] += 1
            await bus.publish(run_id, {"v": 1, "run_id": run_id, "seq": seq["n"],
                                       "ts": _t.time(), "type": event_type,
                                       "iteration": 0, "data": data})

        await emit("run_start", {"message": "(fast-path)"})
        await emit("tool_selection", {
            "mode": "fast-path", "count": 0, "selected": [], "diag": {"via": "fast-path"}})
        await emit("model_turn", {
            "model": "fast-path", "content": text, "tool_calls": []})
        await emit("run_finish", {
            "status": "ok", "answer": text, "iterations": 0,
            "cost_usd": 0, "total_tokens": 0,
            "latency_ms": int((_t.time() - t0) * 1000)})

    async def _slash_reply(run_id: str, command: str, request: Request,
                           conversation_id: str | None, project_id: str | None):
        """Slash commands (/help, /<tool>): no model involved. Same SSE shape as
        _fast_reply; a slashed tool call runs directly, confirmation gate intact."""
        import time as _t
        from runtime.budget import Budget
        from runtime.slash import run_slash
        from runtime.tool_base import ToolContext
        t0 = _t.time()
        owner = _owner(request)
        seq = {"n": 0}

        async def emit(event_type: str, data: dict) -> None:
            seq["n"] += 1
            await bus.publish(run_id, {"v": 1, "run_id": run_id, "seq": seq["n"],
                                       "ts": _t.time(), "type": event_type,
                                       "iteration": 0, "data": data})

        if project_id:
            _wr = PJ.files_root(projects_dir, owner, os.path.basename(project_id))
        else:
            _wr = _scratch_root(owner, conversation_id)
        bcfg = runtime.config.get("budgets") or {}
        budget = Budget(max_iterations=1,
                        max_wall_clock_s=float(bcfg.get("max_wall_clock_s") or 0),
                        max_cost_usd=float(bcfg.get("max_cost_usd") or 1.0),
                        max_total_tokens=int(bcfg.get("max_total_tokens") or 100000))
        ctx = ToolContext(request_id=run_id, config=runtime.config, budget=budget,
                          owner=owner, work_root=str(_wr) if _wr else None,
                          vision_enabled=getattr(runtime, "vision_enabled", False))

        async def confirm(name: str, args: dict) -> bool:
            if provider is None:
                return False
            async def _em(t, i, d):
                await emit(t, d)
            return bool(await provider.confirm(run_id, name, args, _em))

        await emit("run_start", {"message": command})
        await emit("tool_selection", {
            "mode": "slash", "count": 0, "selected": [], "diag": {"via": "slash"}})
        try:
            answer = await run_slash(command, runtime.registry, ctx, confirm)
        except Exception as e:
            answer = f"**error** — {type(e).__name__}: {e}"
        await emit("model_turn", {"model": "slash", "content": answer, "tool_calls": []})
        await emit("run_finish", {
            "status": "ok", "answer": answer, "iterations": 0,
            "cost_usd": 0, "total_tokens": 0,
            "latency_ms": int((_t.time() - t0) * 1000)})

    async def _compact_reply(run_id: str, command: str, request: Request,
                             history: list[dict]):
        """/compact [focus]: summarize the client-owned chat history with the
        local brain and hand back the replacement context — summary + verbatim
        tail — as the run_finish `compact` payload; the client swaps its turn
        list for it. One model call, no tools, no agent loop."""
        import time as _t
        from runtime import compact as _cp
        t0 = _t.time()
        seq = {"n": 0}

        async def emit(event_type: str, data: dict) -> None:
            seq["n"] += 1
            await bus.publish(run_id, {"v": 1, "run_id": run_id, "seq": seq["n"],
                                       "ts": _t.time(), "type": event_type,
                                       "iteration": 0, "data": data})

        async def _finish(answer: str, status: str = "ok", tokens: int = 0,
                          payload: dict | None = None, model: str = "compact"):
            await emit("model_turn", {"model": model, "content": answer,
                                      "tool_calls": []})
            data = {"status": status, "answer": answer, "iterations": 0,
                    "cost_usd": 0, "total_tokens": tokens,
                    "latency_ms": int((_t.time() - t0) * 1000)}
            if payload:
                data["compact"] = payload
            await emit("run_finish", data)

        instruction = command.strip()[len("/compact"):].strip()
        older, kept = _cp.slice_history(history)
        await emit("run_start", {"message": command})
        await emit("tool_selection", {
            "mode": "compact", "count": 0, "selected": [], "diag": {"via": "compact"}})
        if not older:
            await _finish(_cp.nothing_note(len(history or [])))
            return
        try:
            r = await runtime.complete(
                _cp.build_summary_messages(older, instruction), think=False)
            if not r["content"]:
                raise RuntimeError("the brain returned an empty summary")
        except Exception as e:
            await _finish(f"**compact failed** — {type(e).__name__}: {e}",
                          status="error")
            return
        payload = {"summary": r["content"], "kept_messages": len(kept),
                   "dropped_messages": len(older),
                   "instruction": instruction or None}
        display = r["content"] + _cp.result_footer(len(older), len(kept),
                                                   r["usage"])
        await _finish(display, tokens=int(r["usage"].get("total_tokens") or 0),
                      payload=payload, model=runtime.model)

    # ---- chat / stream ----
    @app.post("/api/chat")
    async def chat(req: ChatRequest, request: Request):
        run_id = uuid.uuid4().hex
        u = _user(request)

        def _cleanup(_t: asyncio.Task) -> None:
            async def forget_later():
                await asyncio.sleep(_FORGET_AFTER_S)
                bus.forget(run_id)
                tasks.pop(run_id, None)
                run_owner.pop(run_id, None)
            asyncio.create_task(forget_later())

        # ---- Slash commands: /compact, /help, /<tool> — no agent loop ----
        _sl = req.message.strip()
        if _sl.startswith("/") and not req.attachments:
            if _sl == "/compact" or _sl.startswith("/compact "):
                # Summarizes the chat with the local brain (one call) — the
                # only slash command that touches a model; it needs history.
                coro = _compact_reply(run_id, _sl, request, req.history or [])
            else:
                coro = _slash_reply(run_id, _sl, request, req.conversation_id,
                                    req.project_id)
            task = asyncio.create_task(coro)
            tasks[run_id] = task
            run_owner[run_id] = _owner(request)
            task.add_done_callback(_cleanup)
            return {"run_id": run_id}

        # ---- Fast-path: instant reply for greetings/thanks/bye ----
        qr = quick_reply.match(req.message, u.get("username", ""))
        if qr and not req.attachments and not req.project_id:
            # Same bookkeeping as a real run: run_owner gates /api/stream, and
            # the done-callback retires the task + replay buffer afterwards.
            task = asyncio.create_task(_fast_reply(run_id, qr, _owner(request)))
            tasks[run_id] = task
            run_owner[run_id] = _owner(request)
            task.add_done_callback(_cleanup)
            return {"run_id": run_id}

        # Opportunistic, throttled cleanup of expired unsaved outputs + abandoned scratch.
        if time.time() - _sweep_state["last"] > 600:
            _sweep_state["last"] = time.time()
            try:
                sweep(outputs_dir, output_ttl_hours)
                sweep_scratch(chat_scratch_dir, chat_scratch_ttl_hours)
            except Exception:
                pass
        disabled = set(users.get_global_disabled_tools())
        all_names = [t.name for t in runtime.registry.all()]
        enabled = [n for n in all_names if n not in disabled]
        # If the UI sent an explicit tool list, intersect with enabled.
        # If not (req.tools is None), pass None so the auto-selector can run.
        # If there are globally disabled tools, pass enabled as the filter.
        if req.tools is not None:
            allow = [n for n in req.tools if n in enabled]
        elif disabled:
            allow = enabled  # let selector pick from enabled subset
        else:
            allow = None     # let selector decide freely

        async def on_event(event: dict) -> None:
            await bus.publish(run_id, event)

        message = _augment_with_attachments(request, req.message, req.attachments)
        message = _augment_with_project(request, message, req.project_id)
        images = (_image_urls_for(request, req.attachments)
                  if getattr(runtime, "vision_enabled", False) else None)
        owner = _owner(request)
        # The agent's structural workspace for this run: the active project's
        # files dir, else this chat's owner-scoped scratch dir. None -> the run
        # falls back to its ephemeral per-run tmp only.
        if req.project_id:
            _wr = PJ.files_root(projects_dir, owner, os.path.basename(req.project_id))
        else:
            _wr = _scratch_root(owner, req.conversation_id)
        work_root = str(_wr) if _wr else None
        # ---- budget governance ---------------------------------------------
        # Ceilings for this run layer as: admin-set global defaults (runtime.yaml
        # + persisted budget-defaults.json) < per-user account defaults (the
        # /account page) < this request's overrides. Each layer may only TIGHTEN
        # the one below (per-key min) — a user can tighten their own runs but no
        # layer can raise a ceiling past what the admin granted. Special case:
        # max_wall_clock_s 0 means "no ceiling", so a positive value tightens it.
        def _tighter(k: str, cur: float, new: float) -> float:
            if k == "max_wall_clock_s":
                return new if not cur else (cur if not new else min(cur, new))
            return min(cur, new)

        global_budget = runtime.config.get("budgets") or {}
        run_budget = {k: global_budget[k] for k in _BUDGET_KEYS
                      if global_budget.get(k) is not None}
        if u["username"] != "_token":      # token sessions have no account page
            for k, v in users.get_budget_defaults(u["username"]).items():
                if k in run_budget:
                    run_budget[k] = _tighter(k, run_budget[k], v)
                else:
                    run_budget[k] = v
        for k, v in _coerce_budget(req.budget_overrides or {}).items():
            if k not in run_budget:
                run_budget[k] = v
            else:
                run_budget[k] = _tighter(k, run_budget[k], v)
        coro = runtime.run(
            message,
            share_private=req.share_private,
            auto_confirm=req.auto_confirm,
            think=req.think,
            grill=req.grill,
            tools=allow,
            disabled_tools=disabled,
            budget_overrides=run_budget,
            run_overrides={"compaction": req.compaction,
                           "parallel_tools": req.parallel_tools,
                           "sampling": req.sampling,
                           "sub_budget": req.sub_budget,
                           "architect_threshold": req.architect_threshold},
            run_id=run_id,
            on_event=on_event,
            confirm_provider=provider,
            ask_provider=qprovider,
            history=req.history,
            owner=owner,
            work_root=work_root,
            images=images,
            stream=True,
        )
        task = asyncio.create_task(coro)
        tasks[run_id] = task
        run_owner[run_id] = _owner(request)
        task.add_done_callback(_cleanup)
        return {"run_id": run_id}

    # ---- voice channel (native/voice clients; server-managed conversation) ----
    def _history_from_turns(turns: list[dict]) -> list[dict]:
        h: list[dict] = []
        for t in turns:
            if t.get("user_message"):
                h.append({"role": "user", "content": t["user_message"]})
            h.append({"role": "assistant", "content": t.get("answer", "")})
        return h

    @app.post("/api/voice")
    async def voice(req: VoiceRequest, request: Request):
        vcfg = runtime.config.get("voice", {}) or {}
        if not vcfg.get("enabled", False):
            raise HTTPException(status_code=404, detail="voice channel disabled")
        if not (req.text or "").strip():
            raise HTTPException(status_code=400, detail="empty text")
        owner = _owner(request)

        # Continue an existing owned conversation, else start a fresh one. A
        # supplied id is only honoured if it belongs to this user (no cross-user
        # writes); otherwise a new id is minted and returned.
        cid = req.conversation_id
        chat = chats.get(cid, owner) if (cid and owner is not None) else None
        if chat:
            turns = list(chat["turns"]); conversation_id = cid
        else:
            turns = []; conversation_id = uuid.uuid4().hex
        project_id = (chat or {}).get("project_id")

        # Voice command: "create a project [called X]". Deterministic — promote
        # this conversation into a project (sweeping any files made so far),
        # bind the chat to it, and confirm by voice. No agent run needed.
        if owner is not None and _CREATE_PROJECT_RE.match(req.text or ""):
            m = _CREATE_PROJECT_RE.match(req.text)
            name = (m.group("name") or "").strip().strip('"\'.')
            if not name:
                name = (chat or {}).get("title") or "Voice project"
            meta = _promote_chat_to_project(owner, {"id": conversation_id,
                    "title": (chat or {}).get("title"), "turns": turns}, name)
            reply = (f"Done. I've created the project {meta['name']} and saved this "
                     f"conversation and its files into it. Anything we make from here "
                     f"goes in there too.")
            turns.append({"user_message": req.text, "answer": reply,
                          "run_id": None, "status": "ok"})
            chats.upsert(conversation_id, (chat or {}).get("title"), turns,
                         owner=owner, project_id=meta["id"])
            return {"conversation_id": conversation_id, "run_id": None,
                    "text": reply, "status": "ok", "project_id": meta["id"]}

        # Safe unattended toolset: drop confirmation-gated tools and cloud
        # llm.call (no UI to approve them on a voice turn), and the user's own
        # disabled tools. The brain still answers directly; tools are optional.
        gated = {t.name for t in runtime.registry.all()
                 if getattr(t, "requires_confirmation", False)}
        remote = set((runtime.config.get("privacy", {}) or {}).get("remote_llm_tools", []))
        disabled = set(users.get_global_disabled_tools())
        allow = [t.name for t in runtime.registry.all()
                 if t.name not in gated and t.name not in remote and t.name not in disabled]

        run_id = uuid.uuid4().hex
        vbudget = vcfg.get("budget") or None

        async def on_event(event: dict) -> None:
            await bus.publish(run_id, event)

        async def _go():
            # If this conversation is bound to a project, give the agent the
            # project context so its work centres there.
            msg = _augment_with_project(request, req.text, project_id) if project_id else req.text
            _wr = (PJ.files_root(projects_dir, owner, os.path.basename(project_id))
                   if project_id else _scratch_root(owner, conversation_id))
            result = await runtime.run(
                msg, think=False, extra_system=vcfg.get("persona"),
                budget_overrides=vbudget, model=vcfg.get("model"),
                tools=allow, run_id=run_id, on_event=on_event,
                confirm_provider=provider, ask_provider=qprovider,
                history=_history_from_turns(turns),
                owner=owner, work_root=(str(_wr) if _wr else None), stream=True)
            if owner is not None:   # persist the turn for continuity
                turns.append({"user_message": req.text, "answer": result.get("answer", ""),
                              "run_id": run_id, "status": result.get("status")})
                try:
                    chats.upsert(conversation_id, None, turns,
                                 owner=owner, project_id=project_id)
                except Exception:
                    pass
                # Keep files made this turn inside the project.
                if project_id:
                    try:
                        _sweep_outputs_into_project(owner, [run_id], project_id)
                    except Exception:
                        pass
            return result

        if req.stream:
            task = asyncio.create_task(_go())
            tasks[run_id] = task
            run_owner[run_id] = owner

            def _cleanup(_t: asyncio.Task) -> None:
                async def forget_later():
                    await asyncio.sleep(_FORGET_AFTER_S)
                    bus.forget(run_id); tasks.pop(run_id, None); run_owner.pop(run_id, None)
                asyncio.create_task(forget_later())
            task.add_done_callback(_cleanup)
            # app opens GET /api/stream/{run_id} (same Bearer token) for tokens
            return {"conversation_id": conversation_id, "run_id": run_id}

        run_owner[run_id] = owner
        try:
            result = await _go()
        finally:
            run_owner.pop(run_id, None)
        return {"conversation_id": conversation_id, "run_id": run_id,
                "text": result.get("answer", ""), "status": result.get("status")}

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

    @app.post("/api/answer/{run_id}")
    async def answer(run_id: str, req: AnswerRequest, request: Request):
        if not _can_access_run(request, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        ok = qprovider.resolve(run_id, req.ask_id, {"answers": req.answers})
        if not ok:
            raise HTTPException(status_code=404, detail="no pending questions")
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

    # ---- tools (global admin-controlled enable/disable) ----
    @app.get("/api/tools")
    async def list_tools(request: Request):
        disabled = set(users.get_global_disabled_tools())
        out = []
        for t in sorted(runtime.registry.all(), key=lambda x: x.name):
            out.append({
                "name": t.name,
                "namespace": t.name.split(".")[0],
                "description": getattr(t, "description", "") or "",
                "private": bool(getattr(t, "private", False)),
                "requires_confirmation": bool(getattr(t, "requires_confirmation", False)),
                "parameters": getattr(t, "parameters", {}) or {},
                "enabled": t.name not in disabled,
            })
        return {"tools": out}

    @app.post("/api/tools")
    async def save_tools(req: ToolsRequest, request: Request):
        u = _user(request)
        # Only admins can toggle tools globally
        if not u.get("is_admin"):
            raise HTTPException(403, "Only admins can change tool toggles")
        users.set_global_disabled_tools(req.disabled)
        return {"ok": True, "disabled": sorted(set(req.disabled))}

    # ---- saved chats (per user) ----
    @app.get("/api/chats")
    async def list_chats(request: Request):
        return {"chats": chats.list(owner=_owner(request))}

    @app.post("/api/chats")
    async def save_chat(req: SaveChatRequest, request: Request):
        result = chats.upsert(req.id, req.title, [t.model_dump() for t in req.turns],
                              owner=_owner(request), project_id=req.project_id)
        if result is None:   # id exists under a different owner — don't reveal it
            raise HTTPException(status_code=404, detail="no such chat")
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
        u = _user(request)
        if not chats.rename(chat_id, req.title, owner=_owner(request),
                            is_admin=bool(u["is_admin"])):
            raise HTTPException(status_code=404, detail="no such chat")
        return {"ok": True}

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: str, request: Request):
        owner = _owner(request)
        u = _user(request)
        existing = chats.get(chat_id, owner=owner)   # read run_ids before delete
        if not chats.delete(chat_id, owner=owner, is_admin=bool(u["is_admin"])):
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

    @app.get("/api/admin/budget-defaults")
    async def get_budget_defaults_admin():
        b = runtime.config.get("budgets", {})
        return {k: b.get(k) for k in _BUDGET_KEYS}

    @app.put("/api/admin/budget-defaults")
    async def put_budget_defaults_admin(req: BudgetDefaultsRequest):
        vals = _coerce_budget(req.model_dump())
        if not vals:
            raise HTTPException(status_code=400,
                                detail="provide at least one positive budget value")
        runtime.config["budgets"].update(vals)   # immediate effect for new runs
        try:
            cur = {}
            if budget_defaults_path.exists():
                cur = json.loads(budget_defaults_path.read_text())
            cur.update(vals)
            budget_defaults_path.parent.mkdir(parents=True, exist_ok=True)
            budget_defaults_path.write_text(json.dumps(cur, indent=2))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"could not persist: {e}")
        return {k: runtime.config["budgets"].get(k) for k in _BUDGET_KEYS}

    # ---- admin: config editor (read/write runtime config) ----
    def _flatten_config(d, prefix=""):
        """Flatten a nested dict into dot-path → value pairs."""
        out = {}
        for k, v in (d or {}).items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.update(_flatten_config(v, path))
            else:
                out[path] = v
        return out

    def _set_nested(d, dotpath, value):
        """Set a nested dict value from a dot-path string."""
        parts = dotpath.split(".")
        for p in parts[:-1]:
            d = d.setdefault(p, {})
        d[parts[-1]] = value

    def _deep_merge(base, override):
        """Deep merge override into base (mutates base)."""
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                _deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    @app.get("/api/admin/config")
    async def admin_config_get():
        flat = _flatten_config(runtime.config)
        overrides = users.get_config_overrides()
        return {"config": flat, "overrides": overrides}

    @app.put("/api/admin/config")
    async def admin_config_put(request: Request):
        body = await request.json()
        updates = body.get("updates", {})
        if not isinstance(updates, dict):
            raise HTTPException(400, "updates must be a dict of dotpath → value")
        # Persist as overrides (don't modify the YAML file)
        cur = users.get_config_overrides()
        cur.update(updates)
        # Remove entries that are explicitly set to None (reset to default)
        cur = {k: v for k, v in cur.items() if v is not None}
        users.set_config_overrides(cur)
        # Apply live to the running config
        for dotpath, value in updates.items():
            if value is None:
                # Reset: re-read from YAML
                import yaml as _yaml
                orig = _yaml.safe_load(runtime.config_path.open())
                parts = dotpath.split(".")
                v = orig
                for p in parts:
                    v = (v or {}).get(p)
                _set_nested(runtime.config, dotpath, v)
            else:
                _set_nested(runtime.config, dotpath, value)
        return {"ok": True, "applied": len(updates)}

    # ---- admin: global tool toggles ----
    @app.get("/api/admin/disabled-tools")
    async def admin_disabled_tools_get():
        all_tools = sorted(runtime.registry._tools.keys())
        disabled = set(users.get_global_disabled_tools())
        return {
            "tools": [{"name": t, "disabled": t in disabled} for t in all_tools],
            "disabled": sorted(disabled),
        }

    @app.put("/api/admin/disabled-tools")
    async def admin_disabled_tools_put(request: Request):
        body = await request.json()
        disabled = body.get("disabled", [])
        if not isinstance(disabled, list):
            raise HTTPException(400, "disabled must be a list of tool names")
        users.set_global_disabled_tools(disabled)
        return {"ok": True, "disabled": sorted(set(disabled))}

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
                     # /health requires an API key (probing it headerless makes
                     # the proxy log an auth ERROR); /health/liveliness is the
                     # unauthenticated route meant for exactly this.
                     **await probe(runtime.litellm_base + "/health/liveliness")}]
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
                "active_runs": sum(1 for t in tasks.values()
                                   if t is not None and not t.done()),
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

    @app.get("/api/admin/usage")
    async def admin_usage():
        """Per-user usage overview aggregated from the trace runs table."""
        db = runtime.config["trace"]["db_path"]
        if not Path(db).exists():
            return {"users": [], "totals": {"users": 0, "runs": 0, "tokens": 0, "cost": 0.0}}
        conn = sqlite3.connect(db, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT COALESCE(NULLIF(owner,''),'(unknown)') AS user, "
                "COUNT(*) AS runs, "
                "COALESCE(SUM(total_tokens),0) AS tokens, "
                "COALESCE(SUM(cost_usd),0) AS cost, "
                "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok, "
                "SUM(CASE WHEN status NOT IN ('ok') AND status IS NOT NULL THEN 1 ELSE 0 END) AS errors, "
                "MAX(started_at) AS last_active "
                "FROM runs GROUP BY user ORDER BY runs DESC").fetchall()
            users_ = [dict(r) for r in rows]
            for u in users_:
                u["cost"] = round(u["cost"] or 0, 4)
            totals = {
                "users": len(users_),
                "runs": sum(u["runs"] for u in users_),
                "tokens": sum(u["tokens"] for u in users_),
                "cost": round(sum(u["cost"] for u in users_), 4),
            }
            return {"users": users_, "totals": totals}
        finally:
            conn.close()

    @app.get("/api/admin/users")
    async def admin_users():
        return {"users": users.list()}

    @app.post("/api/admin/users")
    async def admin_create_user(req: NewUserRequest):
        if not _USERNAME_RE.match(req.username or ""):
            raise HTTPException(status_code=400, detail="invalid username")
        if users.get(req.username):
            raise HTTPException(status_code=409, detail="user exists")
        if not req.password:
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
        # Revoke the deleted user's credentials and drop their saved chats — a
        # recreated account with the same name must not inherit either.
        users.revoke_all_api_tokens(username)
        chats.delete_owner(username)
        # On-disk dirs stay for the admin to clean up manually (no rmtree).
        leftover = [str(d) for d in (uploads_dir / username, projects_dir / username,
                                     chat_scratch_dir / username) if d.exists()]
        return {"ok": True, "deleted": username, "leftover_paths": leftover}

    # ---- admin: RAG management ----
    def _rag_db() -> str:
        return ((runtime.config.get("tools", {}) or {}).get("rag", {}) or {}).get(
            "db_path", str(RAG_DB))

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

    @app.on_event("startup")
    async def _apply_boot_posture() -> None:
        from runtime.boot_posture import apply_boot_posture
        asyncio.create_task(apply_boot_posture(runtime))

    # ---- managed processes (brain, embed, rerank) ----
    from runtime.process_manager import ProcessManager
    proc_mgr = ProcessManager()
    _proc_cfg = runtime.config.get("processes") or {}
    for pname, pcfg in _proc_cfg.items():
        if not pcfg.get("command"):
            continue
        proc_mgr.add(
            pname, pcfg["command"],
            env=pcfg.get("env") or {},
            cwd=pcfg.get("cwd"),
            restart=pcfg.get("restart", True),
            restart_delay=pcfg.get("restart_delay", 10),
            start_delay=pcfg.get("start_delay", 0),
            kill_signal=pcfg.get("kill_signal", 9),
            max_log_lines=pcfg.get("max_log_lines", 2000),
        )

    @app.on_event("startup")
    async def _start_managed_processes() -> None:
        if proc_mgr.names():
            print(f"[process-manager] starting {len(proc_mgr.names())} processes: {', '.join(proc_mgr.names())}")
            await proc_mgr.start_all()

    @app.on_event("shutdown")
    async def _stop_managed_processes() -> None:
        if proc_mgr.names():
            print(f"[process-manager] stopping all managed processes")
            await proc_mgr.stop_all()

    # ---- scheduled prompts (schedule.* tools) ----
    from runtime.scheduler import ScheduleStore
    sched_cfg = (runtime.config.get("tools") or {}).get("schedule", {}) or {}
    sched_store = ScheduleStore(sched_cfg.get("store", "/srv/data/schedules.json"))

    def _scheduled_chat_turns(owner: str) -> tuple[str | None, list[dict]]:
        """(chat_id, turns) of the owner's '⏰ Scheduled runs' saved chat."""
        for c in chats.list(owner):
            if c["title"] == "⏰ Scheduled runs":
                full = chats.get(c["id"], owner) or {}
                return c["id"], full.get("turns", [])
        return None, []

    async def _fire_scheduled(entry: dict) -> None:
        owner = entry.get("owner")
        prompt = entry.get("prompt", "")
        run_id = uuid.uuid4().hex
        chat_id, _ = _scheduled_chat_turns(owner)
        wr = _scratch_root(owner, chat_id)

        async def on_event(event: dict) -> None:
            await bus.publish(run_id, event)

        task = asyncio.create_task(runtime.run(
            "This is a SCHEDULED run the user set up earlier. Do what the task "
            "says, then give a short, direct report — it lands in the user's "
            "'⏰ Scheduled runs' chat.\n\nTASK:\n" + prompt,
            run_id=run_id, on_event=on_event, owner=owner,
            work_root=str(wr) if wr else None,
            auto_confirm=bool(sched_cfg.get("auto_confirm", True)),
            budget_overrides=sched_cfg.get("budget") or None,
            stream=True))
        tasks[run_id] = task
        run_owner[run_id] = owner
        try:
            out = await task
        finally:
            tasks.pop(run_id, None)
            run_owner.pop(run_id, None)
        chat_id, turns = _scheduled_chat_turns(owner)
        turns.append({"user_message": f"⏰ {prompt}",
                      "answer": out.get("answer", "") if isinstance(out, dict) else "",
                      "run_id": run_id,
                      "status": out.get("status") if isinstance(out, dict) else "error",
                      "events": []})
        chats.upsert(chat_id, "⏰ Scheduled runs", turns, owner=owner)

    async def _scheduler_tick() -> None:
        for entry in sched_store.due()[: int(sched_cfg.get("max_per_tick", 2))]:
            # mark BEFORE firing: crash => at-most-once (never a double-fire)
            sched_store.mark_fired(entry["id"])
            try:
                await _fire_scheduled(entry)
            except Exception as e:
                print(f"[scheduler] run {entry.get('id')} failed: {e}")

    @app.on_event("startup")
    async def _start_scheduler() -> None:
        if not sched_cfg.get("enabled", True):
            return

        async def loop() -> None:
            while True:
                await asyncio.sleep(int(sched_cfg.get("tick_s", 30)))
                try:
                    await _scheduler_tick()
                except Exception as e:
                    print(f"[scheduler] tick failed: {e}")

        asyncio.create_task(loop())
        print(f"[scheduler] enabled (tick {int(sched_cfg.get('tick_s', 30))}s, "
              f"store {sched_store.path})")

    # ---- admin: process management ----
    def _metrics_port(name: str) -> int | None:
        """The llama-server port for a managed process, via models.presets
        (process names mirror preset names: brain/coder/embed/rerank)."""
        p = (runtime.config.get("models") or {}).get("presets") or {}
        try:
            port = (p.get(name) or {}).get("port")
            return int(port) if port else None
        except (TypeError, ValueError):
            return None

    async def _proc_stats(name: str, alive: bool) -> dict:
        """Stats for the process card: MTP acceptance parsed from the ring-buffer
        logs, plus cumulative counters from the server's own /metrics endpoint
        (uptime window — everything since the process booted)."""
        st = proc_mgr.stats(name)
        port = _metrics_port(name)
        if not port or not alive:
            return st
        try:
            async with httpx.AsyncClient(timeout=1.5) as c:
                r = await c.get(f"http://127.0.0.1:{port}/metrics")
            m = _parse_llama_metrics(r.text)
        except Exception:
            return st                       # server busy/down: log stats only
        gen_tok = m.get("tokens_predicted_total", 0.0)
        gen_s = m.get("tokens_predicted_seconds_total", 0.0)
        pp_tok = m.get("prompt_tokens_total", 0.0)
        pp_s = m.get("prompt_seconds_total", 0.0)
        if gen_s > 0:
            st["gen_tps_avg"] = round(gen_tok / gen_s, 1)
        if pp_s > 0:
            st["prompt_tps_avg"] = round(pp_tok / pp_s, 1)
        st["tokens_generated"] = int(gen_tok)
        st["tokens_prompt"] = int(pp_tok)
        return st

    @app.get("/api/admin/processes")
    async def admin_processes():
        data = proc_mgr.status()
        async def _enrich(item):
            name, s = item
            return name, {**s, "stats": await _proc_stats(name, bool(s.get("alive")))}
        pairs = await asyncio.gather(*(_enrich(i) for i in data.items()))
        return dict(pairs)

    @app.get("/api/admin/processes/{name}/logs")
    async def admin_process_logs(name: str, lines: int = 200):
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        return {"name": name, "lines": proc_mgr.logs(name, lines)}

    @app.post("/api/admin/processes/{name}/restart")
    async def admin_process_restart(name: str):
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        await proc_mgr.stop_one(name)
        await asyncio.sleep(1)
        await proc_mgr.start_one(name)
        return {"ok": True, "name": name}

    @app.post("/api/admin/processes/{name}/stop")
    async def admin_process_stop(name: str):
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        await proc_mgr.stop_one(name)
        return {"ok": True, "name": name}

    @app.post("/api/admin/processes/{name}/start")
    async def admin_process_start(name: str):
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        await proc_mgr.start_one(name)
        return {"ok": True, "name": name}

    return app


_CONFIG = os.environ.get("ORCH_CONFIG") or str(__import__("runtime.paths", fromlist=["CONFIG"]).CONFIG)
app = create_app(_CONFIG) if Path(_CONFIG).exists() else None
