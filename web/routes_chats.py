"""Saved chats, current-chat sync and session flagging routes (split out of
web/server.py)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from fastapi import HTTPException, Request

from runtime.outputs import delete_output, mark_saved
from web import watchdog as watchdog_mod
from web.models import (
    _MINTED_RUN_ID,
    CurrentChatRequest,
    FlagRequest,
    RenameRequest,
    SaveChatRequest,
)


async def _stt_forward(url: str, filename: str, data: bytes) -> str:
    """POST the browser-encoded WAV to the whisper slot; returns the
    transcript text. Module-level so tests can patch this seam (patching
    httpx.AsyncClient itself would break the test client too)."""
    import httpx
    async with httpx.AsyncClient(timeout=300) as cl:
        r = await cl.post(url, data={"response-format": "json"},
                          files={"file": (filename, data, "audio/wav")})
        r.raise_for_status()
        return str((r.json() or {}).get("text") or "").strip()


def register(app, s):
    runtime = s.runtime
    chats = s.chats
    flags = s.flags
    reports = s.reports
    outputs_dir = s.outputs_dir
    _user = s._user
    _owner = s._owner

    # ---- speech-to-text (mic button → local whisper stt slot) ----
    # The browser records webm/opus and re-encodes to 16 kHz mono WAV
    # client-side (whisper.cpp has no ffmpeg); this route just forwards the
    # WAV to the stt slot. Local-only — nothing here ever leaves the box.
    def _stt_url() -> str:
        return (str((runtime.config.get("tools", {}).get("audio", {}) or {})
                    .get("stt_url") or "").strip()
                or "http://127.0.0.1:8099/inference")

    @app.get("/api/stt")
    async def stt_status(request: Request):
        """Mic-button probe: the button only appears when the whisper slot is
        actually reachable (cheap TCP connect, no model call)."""
        from urllib.parse import urlparse
        u = urlparse(_stt_url())
        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(u.hostname, u.port or 80), 1.5)
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
            return {"available": True}
        except Exception:
            return {"available": False}

    _STT_MAX = 25 * 1024 * 1024

    @app.post("/api/stt")
    async def stt_transcribe(request: Request):
        form = await request.form()
        f = form.get("file")
        if f is None:
            raise HTTPException(400, "missing audio file")
        data = await f.read()
        if not data:
            raise HTTPException(400, "empty audio")
        if len(data) > _STT_MAX:
            raise HTTPException(413, "audio too large (25MB max)")
        try:
            text = await _stt_forward(_stt_url(),
                                      getattr(f, "filename", "") or "mic.wav",
                                      data)
        except Exception as e:
            import httpx
            if isinstance(e, httpx.HTTPStatusError):
                raise HTTPException(
                    502, f"whisper returned HTTP {e.response.status_code}")
            raise HTTPException(
                503, "stt slot not reachable — assign a whisper preset in "
                     f"Admin → Presets ({type(e).__name__})")
        return {"text": text}

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
        # run_ids in turns are client-supplied — owner-scoped so a forged id
        # can't pin another user's files.
        owner = _owner(request)
        for t in req.turns:
            if t.run_id:
                mark_saved(outputs_dir, t.run_id, True, owner=owner)
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
                delete_output(outputs_dir, t["run_id"], owner=owner)
        return {"ok": True, "deleted": chat_id}

    # ---- current chat: the active (possibly unsaved) chat, synced per user --
    # Lets the same in-progress session follow the user across browsers and
    # devices instead of living in one browser's localStorage. The payload is
    # the client's chat snapshot, stored verbatim; last writer wins. Token
    # sessions have no account — they all share the "_token" row (same
    # convention as _owner_dir).
    def _current_owner(request: Request) -> str:
        return _owner(request) or "_token"

    @app.get("/api/current-chat")
    async def get_current_chat(request: Request):
        row = chats.get_current(_current_owner(request))
        return row or {"chat": None, "active_run": None, "updated_at": None}

    @app.put("/api/current-chat")
    async def put_current_chat(req: CurrentChatRequest, request: Request):
        if req.chat is None:   # explicit empty state == cleared session
            chats.clear_current(_current_owner(request))
            return {"ok": True, "updated_at": None}
        return chats.set_current(_current_owner(request), req.chat,
                                 active_run=req.active_run)

    @app.delete("/api/current-chat")
    async def delete_current_chat(request: Request):
        chats.clear_current(_current_owner(request))
        return {"ok": True}

    # ---- flag this session for admin debugging ------------------------------
    # The user marks a broken session ("lots of failed tool calls"); the admin
    # gets a privacy-safe structural log in the Flags tab. Only runs that
    # actually belong to the caller can be attached — the flag never grants
    # access to anyone else's traces.
    @app.post("/api/flag")
    async def flag_session(req: FlagRequest, request: Request):
        owner = _current_owner(request)
        ids = [r for r in dict.fromkeys(req.run_ids)
               if _MINTED_RUN_ID.match(r or "")][:50]
        if not ids:
            raise HTTPException(status_code=400, detail="no runs to flag yet")
        keep = []
        db = runtime.config["trace"]["db_path"]
        if Path(db).exists():
            conn = sqlite3.connect(db, timeout=10)
            try:
                q = ",".join("?" * len(ids))
                if owner == "_token":   # token runs are traced with owner NULL
                    rows = conn.execute(
                        f"SELECT id FROM runs WHERE id IN ({q}) AND owner IS NULL",
                        ids).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT id FROM runs WHERE id IN ({q}) AND owner=?",
                        (*ids, owner)).fetchall()
                keep = [r[0] for r in rows]
            finally:
                conn.close()
        if not keep:
            raise HTTPException(status_code=400,
                                detail="none of these runs belong to you")
        flag = flags.create(owner, keep, comment=(req.comment or "")[:2000],
                            conversation_id=req.conversation_id,
                            chat_title=(req.chat_title or "")[:120] or None,
                            include_private=bool(req.include_private))

        async def _coroner_pass() -> None:
            # Watchdog: attach a post-mortem to the flagged runs (background —
            # the flag response doesn't wait on the brain).
            try:
                await watchdog_mod.attach_to_flag(
                    runtime, reports, db, owner, keep)
            except Exception:
                pass
        asyncio.create_task(_coroner_pass())
        return {"ok": True, "flag_id": flag["id"], "runs": len(keep)}
