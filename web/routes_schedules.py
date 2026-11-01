"""Admin routes for the prompt scheduler: list/create/enable/delete schedule
entries, per-schedule run history, and the per-user schedules_allowed flag
(split out of web/server.py).

Same shape as web/routes_eval.py — the admin gate is the auth middleware in
web/server.py (everything here is under /api/admin/*); no per-route decorator.
Entries live in the JSON ScheduleStore (runtime/scheduler.py), history in the
schedule_runs table of chats.db (web/store.py ScheduleRunStore). The ticker in
web/routes_procs.py fires due entries; these routes only manage the rows.
"""

from __future__ import annotations

import os
import time

from fastapi import HTTPException

from runtime import paths as _paths
from runtime.scheduler import ScheduleStore, parse_cron, parse_every, parse_when
from runtime.trigger_store import TriggerStore
from runtime.work_store import WorkStore
from web import projects as PJ
from web.models import (
    ScheduleCreateRequest,
    ScheduleEnabledRequest,
    SchedulesAllowedRequest,
)
from web.trigger_runtime import OBSERVE_MIN_INTERVAL_S, observe_mandate


def _source_fields(req: ScheduleCreateRequest, entry: dict) -> dict:
    if req.source is None:
        return {}
    if req.source != "arxiv":
        raise HTTPException(status_code=400, detail="source must be 'arxiv'")
    if req.kind != "every":
        raise HTTPException(status_code=400,
                            detail="an arXiv watch must use kind='every'")
    if entry["every_s"] < OBSERVE_MIN_INTERVAL_S:
        raise HTTPException(status_code=400,
                            detail="arXiv watches require an interval of at least 15m")
    query = (req.query or "").strip()
    if not query or len(query) > 500:
        raise HTTPException(status_code=400,
                            detail="an arXiv query of 1-500 characters is required")
    return observe_mandate(entry) | {"query": query}


def _new_schedule(req: ScheduleCreateRequest, users, projects_dir) -> dict:
    if users.get(req.owner) is None:
        raise HTTPException(status_code=400, detail=f"no such user: {req.owner}")
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    # created_by="admin" marks the entry as admin-planted: the ticker's
    # revocation sweep leaves it alone when the owner's schedules flag is off.
    entry: dict = {"owner": req.owner, "prompt": prompt, "kind": req.kind,
                   "project_id": None, "enabled": bool(req.enabled),
                   "created_by": "admin"}
    try:
        if req.kind == "once":
            entry["next_fire"] = parse_when(req.when or "")
        elif req.kind == "every":
            entry["every_s"] = parse_every(req.every or "")
            entry["next_fire"] = time.time() + entry["every_s"]
        elif req.kind == "cron":
            entry["cron"] = (req.cron or "").strip()
            entry["next_fire"] = parse_cron(entry["cron"], time.time())
        else:
            raise HTTPException(status_code=400,
                                detail=f"unknown kind {req.kind!r} (once|every|cron)")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    entry.update(_source_fields(req, entry))
    if req.project_id:
        pid = os.path.basename(req.project_id)   # no traversal in the id
        if PJ.files_root(projects_dir, req.owner, pid) is None:
            raise HTTPException(status_code=400,
                                detail=f"no project {req.project_id!r} for {req.owner}")
        entry["project_id"] = pid
    return entry


def _set_enabled(schedules: ScheduleStore, work: WorkStore,
                 sid: str, enabled: bool) -> bool:
    if not schedules.set_enabled(sid, enabled):
        return False
    if not enabled:
        work.cancel_source(sid, "schedule disabled before launch")
    return True


def register(app, s):
    runtime = s.runtime
    users = s.users

    def _sched_store() -> ScheduleStore:
        # Constructed per call (like tools/schedule): the store is a tiny
        # locked JSON read/write, and tests can re-point the config path.
        cfg = (runtime.config.get("tools") or {}).get("schedule", {}) or {}
        return ScheduleStore(cfg.get("store", str(_paths.DATA / "schedules.json")))

    def _work_store() -> WorkStore:
        return WorkStore(s.web_cfg.get("chats_db") or str(_paths.CHATS_DB))

    def _project_name(owner: str | None, pid: str | None) -> str | None:
        if not pid:
            return None
        meta = PJ.read_meta(s.projects_dir, owner, os.path.basename(str(pid)))
        return (meta or {}).get("name")

    def _view(e: dict) -> dict:
        d = dict(e)
        d.setdefault("last_status", None)
        d["project_name"] = _project_name(e.get("owner"), e.get("project_id"))
        return d

    @app.get("/api/admin/schedules")
    async def admin_list_schedules():
        return {"schedules": [_view(e) for e in _sched_store().list()]}

    @app.get("/api/admin/schedules/projects")
    async def admin_schedule_projects(owner: str):
        """The owner's projects for the create form ({id, name})."""
        return {"projects": [{"id": m["id"], "name": m.get("name")}
                             for m in PJ.list_projects(s.projects_dir, owner)]}

    @app.post("/api/admin/schedules")
    async def admin_add_schedule(req: ScheduleCreateRequest):
        entry = _new_schedule(req, users, s.projects_dir)
        return {"ok": True, "schedule": _view(_sched_store().add(entry))}

    @app.put("/api/admin/schedules/{sid}")
    async def admin_set_schedule_enabled(sid: str, req: ScheduleEnabledRequest):
        if not _set_enabled(_sched_store(), _work_store(), sid, req.enabled):
            raise HTTPException(status_code=404, detail="no such schedule")
        return {"ok": True}

    @app.delete("/api/admin/schedules/{sid}")
    async def admin_delete_schedule(sid: str):
        if not _sched_store().remove(sid, None):     # admin: any owner
            raise HTTPException(status_code=404, detail="no such schedule")
        _work_store().cancel_source(sid, "schedule deleted before launch")
        return {"ok": True}

    @app.get("/api/admin/schedules/{sid}/history")
    async def admin_schedule_history(sid: str):
        entry = next((e for e in _sched_store().list() if e.get("id") == sid), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="no such schedule")
        out = {"runs": _work_store().history(schedule_id=sid)}
        if entry.get("mode") == "observe":
            db = s.web_cfg.get("chats_db") or str(_paths.CHATS_DB)
            out["decisions"] = TriggerStore(db).history(sid)
        return out

    @app.post("/api/admin/users/{username}/schedules")
    async def admin_set_schedules_allowed(username: str,
                                          req: SchedulesAllowedRequest):
        if not users.set_schedules_allowed(username, req.allowed):
            raise HTTPException(status_code=404, detail="no such user")
        return {"ok": True}
