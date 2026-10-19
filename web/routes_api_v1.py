"""Versioned REST surface /api/v1 (harness lessons §2 stage 2, docs/api.md
"Protocol v1").

Thin typed wrappers over the unversioned routes — the launcher, the access
gate, the confirmation/answer providers and the SSE responder are all the
SAME objects /api/chat & friends use (attached to app state by
routes_run.register, shared via _sse_run_events), so the two surfaces can
never drift in behavior. The unversioned paths stay supported aliases;
/api/v1 is where the typed contract is guaranteed.

Auth needs nothing new: the middleware already accepts the session cookie OR
a per-user `jn_` Bearer token for every /api/* route.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import Header, HTTPException, Request

from runtime.protocol import PROTOCOL_VERSION
from web.models import AnswerRequest, ApproveRequest, ChatRequest
from web.routes_run import _sse_run_events


def register(app, s):
    runtime = s.runtime
    bus = s.bus
    provider = s.provider
    qprovider = s.qprovider
    tasks = s.tasks
    _user = s._user
    _owner = s._owner
    _can_access_run = s._can_access_run

    def _run_row(run_id: str) -> sqlite3.Row | None:
        """Trace-store row for run_id, or None. Same connection posture as
        the admin log viewers (WAL: readers never block the run's writer)."""
        db = runtime.config["trace"]["db_path"]
        if not Path(db).exists():
            return None
        conn = sqlite3.connect(db, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT id, started_at, finished_at, status, "
                "COALESCE(total_tokens,0) AS total_tokens, "
                "COALESCE(cost_usd,0) AS cost_usd, owner, final_answer "
                "FROM runs WHERE id=?", (run_id,)).fetchone()
        finally:
            conn.close()

    @app.post("/api/v1/runs")
    async def v1_create_run(req: ChatRequest, request: Request):
        # Plain agent turns only: slash commands bypass the agent loop with
        # their own fast-path emitters and stay a /api/chat feature.
        if req.message.strip().startswith("/"):
            raise HTTPException(
                status_code=422,
                detail="slash commands are not supported on /api/v1 — use /api/chat")
        u = _user(request)
        # Same message preparation as /api/chat's agent-run path (attachment
        # context, project binding, vision urls) — minus the web-UI-only
        # quick-reply/smoke/skill-nudge fast paths.
        message = s._augment_with_attachments(request, req.message, req.attachments)
        message = s._augment_with_project(request, message, req.project_id)
        images = (s._image_urls_for(request, req.attachments)
                  if getattr(runtime, "vision_enabled", False) else None)
        run_id, _task = await s.launch_agent_run(
            username=u["username"], message=message, history=req.history,
            conversation_id=req.conversation_id, project_id=req.project_id,
            share_private=req.share_private, auto_confirm=req.auto_confirm,
            think=req.think, req_tools=req.tools,
            req_budget=req.budget_overrides,
            prefs={"compaction": req.compaction,
                   "parallel_tools": req.parallel_tools,
                   "sampling": req.sampling,
                   "sub_budget": req.sub_budget,
                   "architect_threshold": req.architect_threshold},
            images=images)
        return {"run_id": run_id, "v": PROTOCOL_VERSION}

    @app.get("/api/v1/runs/{run_id}")
    async def v1_run_status(run_id: str, request: Request):
        row = _run_row(run_id)
        if row is None:
            # Not in the trace yet: either a run launched moments ago
            # (trace.start_run runs inside the task) or no such run. A live
            # one still answers via the same access gate /api/stream uses.
            if not _can_access_run(request, run_id):
                raise HTTPException(status_code=404, detail="no such run")
            return {"run_id": run_id, "v": PROTOCOL_VERSION,
                    "status": "running", "started_at": None,
                    "finished_at": None, "total_tokens": 0, "cost_usd": 0.0,
                    "owner": s.run_owner.get(run_id)}
        # Unknown AND foreign runs are both 404 — existence must not leak
        # (same rule as _can_access_run: admin or the run's owner).
        if not (_user(request)["is_admin"] or row["owner"] == _owner(request)):
            raise HTTPException(status_code=404, detail="no such run")
        body = {"run_id": row["id"], "v": PROTOCOL_VERSION,
                "status": row["status"], "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "total_tokens": row["total_tokens"],
                "cost_usd": row["cost_usd"], "owner": row["owner"]}
        # The answer text is owner-only — an admin gets run metadata, not
        # another user's content.
        if row["owner"] == _owner(request):
            body["final_answer"] = row["final_answer"]
        return body

    @app.get("/api/v1/runs/{run_id}/events")
    async def v1_run_events(run_id: str, request: Request,
                            last_event_id: str | None = Header(default=None),
                            after_seq: int | None = None):
        if not _can_access_run(request, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        # Last-Event-ID stays the primary resume token; ?after_seq= is the
        # additive v1 alternative for clients that track seq themselves.
        after = after_seq or 0
        if last_event_id and last_event_id.isdigit():
            after = max(after, int(last_event_id))
        return _sse_run_events(bus, run_id, request, after)

    @app.post("/api/v1/runs/{run_id}/cancel")
    async def v1_cancel_run(run_id: str, request: Request):
        if not _can_access_run(request, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        t = tasks.get(run_id)
        if t and not t.done():
            t.cancel()
            return {"ok": True, "cancelled": True}
        return {"ok": True, "cancelled": False}

    @app.post("/api/v1/runs/{run_id}/approve")
    async def v1_approve(run_id: str, req: ApproveRequest, request: Request):
        if not _can_access_run(request, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        if not provider.resolve(run_id, req.confirmation_id, req.approved):
            raise HTTPException(status_code=404, detail="no pending confirmation")
        return {"ok": True}

    @app.post("/api/v1/runs/{run_id}/answer")
    async def v1_answer(run_id: str, req: AnswerRequest, request: Request):
        if not _can_access_run(request, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        if not qprovider.resolve(run_id, req.ask_id, {"answers": req.answers}):
            raise HTTPException(status_code=404, detail="no pending questions")
        return {"ok": True}
