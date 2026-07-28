"""Admin routes: prompt/budget/config editors, tool toggles, status, logs,
flags, watchdog reports, usage, users and RAG (split out of web/server.py)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import httpx
from fastapi import HTTPException, Request

from web.ctx import _BUDGET_KEYS
from web.models import (AdminFlagRequest, BudgetDefaultsRequest,
                        FlagResolveRequest, NewUserRequest, PasswordRequest,
                        PromptRequest, _USERNAME_RE)


def register(app, s):
    runtime = s.runtime
    users = s.users
    chats = s.chats
    flags = s.flags
    reports = s.reports
    tasks = s.tasks
    web_cfg = s.web_cfg
    uploads_dir = s.uploads_dir
    projects_dir = s.projects_dir
    chat_scratch_dir = s.chat_scratch_dir
    budget_defaults_path = s.budget_defaults_path
    _coerce_budget = s._coerce_budget
    _user = s._user
    started_at = time.time()
    from runtime.paths import RAG_DB

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

    # ---- admin: model preset catalog (DB-backed; runtime/preset_store) ----
    from runtime import preset_store as ps

    def _store() -> ps.PresetStore:
        return ps.PresetStore(ps.db_path_for(runtime.config))

    def _presets_payload() -> dict:
        store = _store()
        presets, slots = store.list_full()
        slot_names = list(dict.fromkeys(
            list(ps.SLOTS) + list(slots)
            + list((runtime.config.get("processes") or {}).keys())))
        ids, info = store.get_gpus()
        gpus = [{"id": g, "label": (info.get(g) or {}).get("label") or "",
                 "vram_gib": (info.get(g) or {}).get("vram_gib")} for g in ids]
        import os as _os
        binaries = [{"name": n, "path": e.get("path") or "",
                     "device_env": e.get("device_env") or ps.DEFAULT_DEVICE_ENV,
                     "exists": _os.access(e.get("path") or "", _os.X_OK)}
                    for n, e in store.get_binaries().items()]
        return {"presets": presets, "slots": slots, "slot_names": slot_names,
                "gpus": gpus, "binaries": binaries,
                # alias+port of a static preset must have a matching entry there
                "litellm_note": "static alias+port must match litellm.yaml"}

    def _check_device(store: ps.PresetStore, body: dict) -> None:
        """Validate/expand body['gpu'] against the topology (400 on bad ids).
        The UI sends "all" for a full split — expand it to the explicit list."""
        if "gpu" not in body:
            return
        raw = str(body.get("gpu") or "").strip().lower()
        ids, _ = store.get_gpus()
        if raw == "all":
            body["gpu"] = ",".join(ids)
            return
        dev = ps.normalize_gpu(raw)          # raises ValueError → 400
        bad = [g for g in ps.gpu_list({"gpu": dev}) if g not in ids]
        if bad:
            raise HTTPException(400, f"unknown GPU id(s): {', '.join(bad)} — "
                                     "add them under GPUs first")
        body["gpu"] = dev

    def _check_binary(store: ps.PresetStore, body: dict) -> None:
        """body['binary'] must name a registry entry ("" = launcher default)."""
        if "binary" not in body:
            return
        b = str(body.get("binary") or "").strip()
        if b and b not in store.get_binaries():
            raise HTTPException(400, f"unknown binary {b!r} — "
                                     "add it under Binaries first")
        body["binary"] = b

    @app.put("/api/admin/gpus")
    async def admin_gpus_put(request: Request):
        body = await request.json()
        rows = body.get("gpus")
        if not isinstance(rows, list):
            raise HTTPException(400, "gpus must be a list of {id, label, vram_gib}")
        ids, info = [], {}
        for r in rows:
            if not isinstance(r, dict):
                raise HTTPException(400, "each GPU must be an object")
            gid = str(r.get("id") or "").strip().lower()
            if not gid:
                raise HTTPException(400, "GPU id may not be empty")
            ids.append(gid)
            info[gid] = {"label": str(r.get("label") or ""),
                         "vram_gib": r.get("vram_gib")}
        try:
            _store().set_gpus(ids, info)
        except ValueError as e:
            raise HTTPException(409, str(e))
        ps.load_into_config(runtime.config)
        return _presets_payload()

    @app.put("/api/admin/binaries")
    async def admin_binaries_put(request: Request):
        body = await request.json()
        rows = body.get("binaries")
        if not isinstance(rows, list):
            raise HTTPException(
                400, "binaries must be a list of {name, path, device_env}")
        bins = {}
        for r in rows:
            if not isinstance(r, dict):
                raise HTTPException(400, "each binary must be an object")
            name = str(r.get("name") or "").strip()
            if not name:
                raise HTTPException(400, "binary name may not be empty")
            bins[name] = {"path": str(r.get("path") or "").strip(),
                          "device_env": str(r.get("device_env") or "").strip()}
        try:
            _store().set_binaries(bins)
        except ValueError as e:
            raise HTTPException(409, str(e))
        ps.load_into_config(runtime.config)
        return _presets_payload()

    # ---- admin: cloud models (DB-backed; runtime/cloud_store) ----
    from runtime import cloud_store as cs
    import os as _os
    from runtime.paths import LITELLM_BASE

    async def _reload_proxy() -> str:
        """Best-effort proxy config reload after a catalog change: LiteLLM's
        /reload endpoint when the running version has it, else a user-service
        restart. Never raises — the string tells the admin what happened."""
        base = ((runtime.config.get("orchestrator") or {}).get("litellm_base")
                or LITELLM_BASE)
        key = _os.environ.get("LITELLM_MASTER_KEY", "")
        try:
            async with httpx.AsyncClient(timeout=3) as cl:
                r = await cl.post(f"{base}/reload",
                                  headers={"Authorization": f"Bearer {key}"})
            if r.status_code < 400:
                return "proxy reloaded"
        except Exception:
            pass
        import asyncio
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "restart", "litellm-proxy",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), 15)
            return ("litellm-proxy restarted" if proc.returncode == 0
                    else "restart litellm-proxy manually to apply")
        except Exception:
            return "restart litellm-proxy manually to apply"

    def _cloud_payload(proxy: str | None = None) -> dict:
        rows = cs.CloudStore(ps.db_path_for(runtime.config)).list()
        for r in rows:
            # boolean only — key VALUES never leave orchestrator.env
            r["key_set"] = bool(r["key_env"]) and bool(
                _os.environ.get(r["key_env"]))
        out = {"models": rows}
        if proxy:
            out["proxy"] = proxy
        return out

    @app.get("/api/admin/cloud-models")
    async def admin_cloud_models_get():
        return _cloud_payload()

    @app.put("/api/admin/cloud-models")
    async def admin_cloud_models_put(request: Request):
        body = await request.json()
        rows = body.get("models")
        if not isinstance(rows, list):
            raise HTTPException(400, "models must be a list of objects")
        try:
            cs.CloudStore(ps.db_path_for(runtime.config)).replace_all(rows)
        except ValueError as e:
            raise HTTPException(400, str(e))
        cs.load_into_config(runtime.config)
        try:
            cs.write_rendered(runtime.config)
            proxy = await _reload_proxy()
        except Exception:
            proxy = "render failed — restart litellm-proxy manually"
        return _cloud_payload(proxy)

    @app.get("/api/admin/presets")
    async def admin_presets_get():
        return _presets_payload()

    @app.post("/api/admin/presets")
    async def admin_presets_create(request: Request):
        body = await request.json()
        store = _store()
        try:
            _check_device(store, body)
            _check_binary(store, body)
            store.upsert((body.get("name") or "").strip(), body,
                         conf=body.get("conf"), create=True)
        except ValueError as e:
            raise HTTPException(400, str(e))
        ps.load_into_config(runtime.config)
        return _presets_payload()

    @app.put("/api/admin/presets/{name}")
    async def admin_presets_update(name: str, request: Request):
        body = await request.json()
        store = _store()
        try:
            _check_device(store, body)
            _check_binary(store, body)
            store.upsert(name, body, conf=body.get("conf"))
        except KeyError:
            raise HTTPException(404, "no such preset")
        except ValueError as e:
            raise HTTPException(400, str(e))
        ps.load_into_config(runtime.config)
        return _presets_payload()

    @app.delete("/api/admin/presets/{name}")
    async def admin_presets_delete(name: str):
        try:
            _store().delete(name)
        except KeyError:
            raise HTTPException(404, "no such preset")
        except ValueError as e:
            raise HTTPException(409, str(e))
        ps.load_into_config(runtime.config)
        return _presets_payload()

    @app.put("/api/admin/preset-slots")
    async def admin_preset_slots_put(request: Request):
        body = await request.json()
        updates = body.get("updates")
        if not isinstance(updates, dict):
            raise HTTPException(400, "updates must be {slot: preset}")
        store = _store()
        try:
            for slot, preset in updates.items():
                store.set_slot(slot, preset)
        except KeyError as e:
            raise HTTPException(404, f"unknown preset: {e.args[0]}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        ps.load_into_config(runtime.config)
        return _presets_payload()

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
        for sv in (web_cfg.get("services") or []):
            services.append({"name": sv.get("name", sv.get("url")), "url": sv.get("url"),
                             **await probe(sv["url"])})

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

    # ---- admin: flagged sessions (privacy-safe debugging) ----
    # Content-bearing keys across event kinds — same set as
    # Trace._strip_content: the admin sees the flow (tool names, errors,
    # iterations, timings), never the user's messages or tool contents.
    _FLAG_STRIP = ("result", "content", "args", "message", "answer",
                   "result_preview", "report")

    def _sanitize_payload(payload_json: str):
        try:
            p = json.loads(payload_json or "{}")
        except Exception:
            p = {}
        if isinstance(p, dict):
            p = {k: ("<stripped>" if k in _FLAG_STRIP else v)
                 for k, v in p.items()}
        return p

    @app.get("/api/admin/flags")
    async def admin_flags():
        return {"flags": flags.list()}

    @app.get("/api/admin/flags/{flag_id}")
    async def admin_flag_detail(flag_id: str):
        flag = flags.get(flag_id)
        if not flag:
            raise HTTPException(status_code=404, detail="no such flag")
        runs_out, missing = [], []
        db = runtime.config["trace"]["db_path"]
        conn = sqlite3.connect(db, timeout=10) if Path(db).exists() else None
        if conn:
            conn.row_factory = sqlite3.Row
        try:
            for rid in (flag["run_ids"] if conn else []):
                # Metadata only: user_message/final_answer stay in the trace DB.
                run = conn.execute(
                    "SELECT id, started_at, finished_at, status, error, "
                    "COALESCE(total_tokens,0) AS total_tokens, "
                    "COALESCE(cost_usd,0) AS cost_usd FROM runs WHERE id=?",
                    (rid,)).fetchone()
                if not run:
                    missing.append(rid)   # pruned by trace retention
                    continue
                events = conn.execute(
                    "SELECT ts, kind, iteration, payload_json FROM events "
                    "WHERE run_id=? ORDER BY id LIMIT 500", (rid,)).fetchall()
                d = dict(run)
                if d.get("finished_at") and d.get("started_at"):
                    d["duration_s"] = round(d["finished_at"] - d["started_at"], 2)
                d["events"] = [{"ts": e[0], "kind": e[1], "iteration": e[2],
                                "payload": _sanitize_payload(e[3])}
                               for e in events]
                runs_out.append(d)
        finally:
            if conn:
                conn.close()
        return {"flag": flag, "runs": runs_out, "missing_runs": missing,
                # Coroner reports for the flagged runs (auto-triggered or
                # written by the flag attach pass).
                "reports": reports.for_runs(flag["run_ids"])}

    # ---- admin: watchdog reports (run coroner) ----
    @app.get("/api/admin/reports")
    async def admin_reports():
        return {"reports": reports.list()}

    @app.delete("/api/admin/reports/{report_id}")
    async def admin_report_delete(report_id: str):
        if not reports.delete(report_id):
            raise HTTPException(status_code=404, detail="no such report")
        return {"ok": True}

    @app.post("/api/admin/flags/{flag_id}/resolve")
    async def admin_flag_resolve(flag_id: str, req: FlagResolveRequest):
        if not flags.set_resolved(flag_id, req.resolved):
            raise HTTPException(status_code=404, detail="no such flag")
        return {"ok": True}

    @app.delete("/api/admin/flags/{flag_id}")
    async def admin_flag_delete(flag_id: str):
        if not flags.delete(flag_id):
            raise HTTPException(status_code=404, detail="no such flag")
        return {"ok": True}

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
                "COALESCE(SUM(cost_usd),0.0) AS cost, "
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
        flags.delete_owner(username)
        reports.delete_owner(username)
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
