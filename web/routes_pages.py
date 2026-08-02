"""Pages, auth API, two-factor and account routes (split out of web/server.py)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from runtime.serve_preset import parse_preset
from web.auth import sign_session
from web.ctx import _BUDGET_KEYS, _COOKIE
from web.models import (ApiTokenRequest, BudgetDefaultsRequest, LoginRequest,
                        PasswordChangeRequest, TimezoneRequest, TwoFACodeRequest)
from web import goals as goals_mod

_STATIC = Path(__file__).parent / "static"


def register(app, s):
    runtime = s.runtime
    users = s.users
    throttle = s.throttle
    secret = s.secret
    cookie_secure = s.cookie_secure
    max_project_file_mb = s.max_project_file_mb
    _user = s._user
    _owner = s._owner

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
        # Effective house defaults for the advanced run knobs, so the account
        # page + quick settings can show real values as placeholders instead of
        # a bare "default" (blank field = these values apply).
        _comp = runtime.config.get("compaction") or {}
        _samp = {k: v for k, v in ((runtime.config.get("orchestrator", {})
                                    .get("sampling") or {}).items())
                 if v is not None}
        _samp.setdefault("temperature", 0.7)   # loop.py brain fallback
        # /imp completion data for the composer's slash preview: local preset
        # names (minus the default brain) + cloud aliases from the cost table
        # (static — no proxy probe on every /api/me).
        _presets = ((runtime.config.get("models") or {}).get("presets") or {})
        _local_aliases = {p.get("alias") for p in _presets.values()} | {runtime.model}
        _imp_models = {
            "local": sorted(n for n, p in _presets.items()
                            if p.get("alias") and p["alias"] != runtime.model),
            "cloud": sorted(a for a in (runtime.cost_table or {})
                            if a not in _local_aliases
                            and not str(a).startswith("local-")),
        }
        _goal = {} if is_token else users.get_goal(u["username"])
        return {"username": u["username"], "is_admin": u["is_admin"],
                "twofa": twofa, "budget": budget,
                "budget_defaults": {k: runtime.config["budgets"].get(k) for k in _BUDGET_KEYS},
                "sub_iterations_default": ((runtime.config.get("agent", {}).get("default_budget") or {}).get("max_iterations")
                                           or runtime.config.get("agent", {}).get("default_sub_iterations", 8)),
                "run_defaults": {
                    "max_result_chars": _comp.get("max_result_chars", 2000),
                    "keep_last": _comp.get("keep_last", 3),
                    "architect_threshold": (runtime.config.get("architect") or {}).get("threshold", 0),
                    "sampling": _samp,
                },
                "vision": bool(getattr(runtime, "vision_enabled", False)),
                "max_file_mb": max_project_file_mb,
                "brain_model": (runtime.brain_info or {}).get("model", ""),
                "brain_override": {} if is_token else users.get_brain_override(u["username"]),
                "imp_models": _imp_models,
                # /goal chip: None when unset, else the live status line's data.
                "goal": ({"objective": _goal.get("objective"),
                          "status": _goal.get("status"),
                          "turn": _goal.get("turn", 0),
                          "max_turns": goals_mod.config(runtime)["max_turns"],
                          "current_run": _goal.get("current_run")}
                         if _goal.get("objective") else None)}

    @app.get("/api/models")
    async def models(request: Request):
        _user(request)  # auth-gate
        orch_cfg = runtime.config.get("orchestrator", {})
        orch_alias = runtime.model
        brain_model = (runtime.brain_info or {}).get("model", "") or orch_alias
        delegate_cfg = (runtime.config.get("tools", {}).get("code", {})
                        .get("delegate", {}) or {})
        specialist_alias = delegate_cfg.get("model")
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
                try:  # underlying model names (e.g. local-specialist -> openai/ornith-…)
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
        if specialist_alias:
            specialist_preset = (os.environ.get("ORCH_SPECIALIST_PRESET")
                                 or delegate_cfg.get("preset"))
            cc = card(specialist_alias, specialist_preset, None)
            if cc["model"] == specialist_alias and underlying.get(specialist_alias):
                cc["model"] = underlying[specialist_alias]   # at least the real backend model
            out["specialist"] = cc
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

    @app.get("/api/account/timezone")
    async def account_timezone_get(request: Request):
        u = _user(request)
        if u["username"] == "_token":
            return {"timezone": ""}
        return {"timezone": users.get_timezone(u["username"])}

    @app.post("/api/account/timezone")
    async def account_timezone_set(req: TimezoneRequest, request: Request):
        u = _user(request)
        if u["username"] == "_token":
            raise HTTPException(status_code=403, detail="token session")
        try:
            users.set_timezone(u["username"], req.timezone)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "timezone": users.get_timezone(u["username"])}

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
