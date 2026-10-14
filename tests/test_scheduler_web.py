"""Scheduler web layer: per-user schedule.* tool gating in the run launcher,
ticker revocation of a disallowed owner, and the fire path (project work_root,
run-history rows). runtime.run is mocked — no real model."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from conftest import run

from runtime.scheduler import ScheduleStore
from web import projects as PJ
from web import routes_procs
from web.store import ScheduleRunStore


# ---- launcher gating (schedule.add/remove need the flag or admin) ------------
def _make_non_admin(app, username="bob", password="bobpass123"):
    app.state.users.create(username, password, is_admin=False)
    return username, password


@pytest.mark.asyncio
async def test_schedule_tools_disabled_for_non_allowed_user(
        web_app, web_client, record_run, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    _make_non_admin(app)
    seen = record_run(app)
    async with web_client(app, username="bob", password="bobpass123") as c:
        r = await c.post("/api/chat", json={"message": "hi"})
        assert r.status_code == 200
        await asyncio.sleep(0.05)
    assert {"schedule.add", "schedule.remove"} <= seen.get("disabled_tools", set())
    # schedule.list stays: read-only, own entries only
    assert "schedule.list" not in seen.get("disabled_tools", set())


@pytest.mark.asyncio
async def test_schedule_tools_kept_for_allowed_user_and_admin(
        web_app, web_client, record_run, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    _make_non_admin(app)
    app.state.users.set_schedules_allowed("bob", True)
    seen = record_run(app)
    async with web_client(app, username="bob", password="bobpass123") as c:
        assert (await c.post("/api/chat", json={"message": "hi"})).status_code == 200
        await asyncio.sleep(0.05)
    assert "schedule.add" not in seen.get("disabled_tools", set())

    seen.clear()
    async with web_client(app) as c:                       # admin
        assert (await c.post("/api/chat", json={"message": "hi"})).status_code == 200
        await asyncio.sleep(0.05)
    assert "schedule.add" not in seen.get("disabled_tools", set())


# ---- tick / fire harness (same shape as tests/test_audit_scheduler.py) ------
class _App:
    def get(self, _path):
        return lambda fn: fn

    def post(self, _path):
        return lambda fn: fn


class _Chats:
    def __init__(self, db_path):
        self.db_path = db_path
        self.upserts = []

    def list(self, owner):
        return []

    def get(self, chat_id, owner):
        return None

    def upsert(self, chat_id, title, turns, owner=None):
        self.upserts.append({"chat_id": chat_id, "turns": turns, "owner": owner})


class _Bus:
    async def publish(self, run_id, event):
        pass


class _Users:
    """Fake UserStore: `allowed` holds the accounts with the schedules flag."""
    def __init__(self, admins=(), allowed=()):
        self._admins = set(admins)
        self._allowed = set(allowed)

    def get_global_disabled_tools(self):
        return []

    def get_schedules_allowed(self, username):
        return username in self._allowed

    def get(self, username):
        if username is None:
            return None
        return {"username": username, "is_admin": username in self._admins}


def _wired(tmp_path, run_impl, users=None, scratch=None):
    """register() against fakes; returns (state, store, run-history store)."""
    store_path = tmp_path / "schedules.json"
    s = SimpleNamespace(
        runtime=SimpleNamespace(
            config={"tools": {"schedule": {"store": str(store_path),
                                           "max_per_tick": 10}}},
            run=run_impl),
        bus=_Bus(), tasks={}, run_owner={}, users=users or _Users(),
        chats=_Chats(str(tmp_path / "chats.db")),
        projects_dir=tmp_path / "projects",
        _scratch_root=lambda owner, chat_id: scratch,
        goal_kick=lambda u: None,
        startup_hooks=[], shutdown_hooks=[],
    )
    routes_procs.register(_App(), s)
    return s, ScheduleStore(str(store_path)), ScheduleRunStore(str(tmp_path / "chats.db"))


async def _wait(pred, tries=300):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


# ---- revocation at the tick --------------------------------------------------
def test_revoked_owner_is_disabled_not_fired(tmp_path, capsys):
    async def main():
        calls = []

        async def fake_run(msg, **kw):
            calls.append(1)
            return {"answer": "", "status": "ok"}

        users = _Users(admins=["root"], allowed=["kim"])
        s, store, runs = _wired(tmp_path, fake_run, users=users)
        revoked = store.add({"owner": "mallory", "prompt": "x", "kind": "every",
                             "every_s": 60, "next_fire": time.time() - 1})
        allowed = store.add({"owner": "kim", "prompt": "y", "kind": "once",
                             "next_fire": time.time() - 1})
        admin = store.add({"owner": "root", "prompt": "z", "kind": "once",
                           "next_fire": time.time() - 1})
        await s.scheduler_tick()
        # 2 fired-run rows (kim + root) + 1 revocation row, all async
        assert await _wait(lambda: len(runs.history()) == 3)
        entries = {e["id"]: e for e in store.list()}
        assert entries[revoked["id"]]["enabled"] is False
        assert entries[allowed["id"]]["enabled"] is False   # one-shot consumed
        rows = runs.history(schedule_id=revoked["id"])
        assert [r["status"] for r in rows] == ["disabled: schedules revoked"]
        assert runs.history(schedule_id=admin["id"])        # fired + recorded

    run(main())
    out = capsys.readouterr().out
    assert "lost schedules access" in out and "mallory" in out


# ---- fire path: project work_root + history ---------------------------------
def test_project_entry_fires_in_project_root_and_records_history(tmp_path):
    async def main():
        seen = {}

        async def fake_run(msg, **kw):
            seen.update(kw)
            return {"answer": "shipped the report", "status": "ok",
                    "total_tokens": 42}

        projects_dir = tmp_path / "projects"
        meta = PJ.create_project(projects_dir, "alice", "Demo")
        s, store, runs = _wired(tmp_path, fake_run,
                             users=_Users(allowed=["alice"]))
        e = store.add({"owner": "alice", "prompt": "report", "kind": "once",
                       "project_id": meta["id"], "next_fire": time.time() - 1})
        await s.scheduler_tick()
        assert await _wait(lambda: bool(runs.history(schedule_id=e["id"])))
        assert seen.get("work_root") == str(
            PJ.files_root(projects_dir, "alice", meta["id"]))
        row = runs.history(schedule_id=e["id"])[0]
        assert row["status"] == "ok" and row["excerpt"] == "shipped the report"
        assert row["tokens"] == 42 and row["run_id"] == seen.get("run_id")
        assert row["duration_s"] is not None
        assert store.list()[0]["last_status"] == "ok"

    run(main())


def test_missing_project_falls_back_to_scratch_with_note(tmp_path):
    async def main():
        seen = {}
        scratch = tmp_path / "scratch" / "alice"

        async def fake_run(msg, **kw):
            seen.update(kw)
            return {"answer": "done", "status": "ok"}

        s, store, runs = _wired(tmp_path, fake_run, scratch=scratch,
                             users=_Users(allowed=["alice"]))
        e = store.add({"owner": "alice", "prompt": "x", "kind": "once",
                       "project_id": "gone-123", "next_fire": time.time() - 1})
        await s.scheduler_tick()
        assert await _wait(lambda: bool(runs.history(schedule_id=e["id"])))
        assert seen.get("work_root") == str(scratch)
        row = runs.history(schedule_id=e["id"])[0]
        assert row["excerpt"].startswith("project missing, used scratch")
        assert "done" in row["excerpt"]

    run(main())


def test_failed_run_records_error_history(tmp_path):
    async def main():
        async def boom(msg, **kw):
            raise RuntimeError("model exploded")

        s, store, runs = _wired(tmp_path, boom,
                             users=_Users(allowed=["alice"]))
        e = store.add({"owner": "alice", "prompt": "x", "kind": "once",
                       "next_fire": time.time() - 1})
        await s.scheduler_tick()
        assert await _wait(lambda: bool(runs.history(schedule_id=e["id"])))
        row = runs.history(schedule_id=e["id"])[0]
        assert row["status"] == "error" and "model exploded" in row["excerpt"]
        assert store.list()[0]["last_status"] == "error"

    run(main())


def test_admin_planted_entry_survives_flag_less_owner(tmp_path):
    """An entry the admin planted for a user without the schedules flag keeps
    firing — the revocation sweep only auto-disables SELF-created entries
    (code audit 2026-10-14)."""
    async def main():
        calls = []

        async def fake_run(msg, **kw):
            calls.append(kw)
            return {"answer": "ok", "status": "ok"}

        s, store, runs = _wired(tmp_path, fake_run,
                                users=_Users(allowed=["kim"]))
        e = store.add({"owner": "mallory", "prompt": "x", "kind": "once",
                       "created_by": "admin", "next_fire": time.time() - 1})
        await s.scheduler_tick()
        assert await _wait(lambda: bool(runs.history(schedule_id=e["id"])))
        row = runs.history(schedule_id=e["id"])[0]
        assert row["status"] == "ok"
        assert calls                                 # fired, not disabled

    run(main())


def test_scheduled_run_gets_deny_confirm_provider(tmp_path):
    """No human at the screen: the fire path passes a confirm provider that
    refuses everything, so the loop never falls back to its TTY prompt on a
    server started from an interactive shell (code audit 2026-10-14)."""
    async def main():
        seen = {}

        async def fake_run(msg, **kw):
            seen.update(kw)
            return {"answer": "x", "status": "ok"}

        s, store, runs = _wired(tmp_path, fake_run,
                                users=_Users(allowed=["alice"]))
        e = store.add({"owner": "alice", "prompt": "x", "kind": "once",
                       "next_fire": time.time() - 1})
        await s.scheduler_tick()
        assert await _wait(lambda: bool(runs.history(schedule_id=e["id"])))
        provider = seen.get("confirm_provider")
        assert provider is not None
        assert await provider.confirm("r", "ops.run", {}) is False

    run(main())
