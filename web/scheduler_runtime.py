"""Durable scheduled-prompt dispatch, recovery, and chat outbox delivery."""

from __future__ import annotations

import asyncio
import os
import time

from runtime.proc import spawn_background
from runtime.scheduler import ScheduleStore
from runtime.work_store import WorkStore
from web import projects as PJ
from web.routes_run import _schedule_tool_gate
from web.store import ScheduleRunStore


def _sched_runs_db(s, chats) -> str:
    from runtime import paths as _paths
    return ((getattr(s, "web_cfg", {}) or {}).get("chats_db")
            or getattr(chats, "db_path", None) or str(_paths.CHATS_DB))


def _schedule_owner_revoked(users, owner: str | None) -> bool:
    u = users.get(owner) if owner else None
    return (u is not None and not u.get("is_admin")
            and not users.get_schedules_allowed(owner))


def _sched_work_root(entry: dict, owner: str | None, chat_id: str | None,
                     projects_dir, scratch_root) -> tuple:
    pid = entry.get("project_id")
    if pid and owner and projects_dir is not None:
        wr = PJ.files_root(projects_dir, owner, os.path.basename(str(pid)))
        if wr is not None:
            return wr, ""
        return scratch_root(owner, chat_id), "project missing, used scratch — "
    return scratch_root(owner, chat_id), ""


def _run_outcome(out) -> tuple[str, str, int | None]:
    if not isinstance(out, dict):
        return "error", "", None
    return ((out.get("status") or "error"), str(out.get("answer", "")),
            out.get("total_tokens"))


class _DenyConfirm:
    """Scheduled runs have no human available for confirmation prompts."""

    async def confirm(self, run_id, name, args, emit=None, reason=None):
        return False


def _record_run(sched_runs, sched_store, entry: dict, run_id: str | None,
                status: str, duration_s: float | None, excerpt: str,
                tokens: int | None) -> None:
    sid = str(entry.get("id") or "?")
    try:
        sched_runs.add(sid, entry.get("owner"), run_id, status,
                       duration_s, excerpt[:500], tokens)
        sched_store.set_status(sid, status)
    except Exception as e:
        print(f"[scheduler] history record for {sid} failed: {e}")


def _drop_revoked(sched_runs, sched_store, users, entries: list) -> list:
    out = []
    for entry in entries:
        planted = entry.get("created_by")
        if planted and planted != entry.get("owner"):
            out.append(entry)
            continue
        if not _schedule_owner_revoked(users, entry.get("owner")):
            out.append(entry)
            continue
        sid = str(entry.get("id"))
        sched_store.set_enabled(sid, False)
        _record_run(sched_runs, sched_store, entry, None,
                    "disabled: schedules revoked", None, "", None)
        print(f"[scheduler] {sid}: owner '{entry.get('owner')}' lost schedules "
              "access — entry disabled")
    return out


class ScheduledRunner:
    def __init__(self, s):
        self.s = s
        self.runtime = s.runtime
        self.bus = s.bus
        self.tasks = s.tasks
        self.run_owner = s.run_owner
        self.users = s.users
        self.chats = s.chats
        self.scratch_root = s._scratch_root
        self.projects_dir = getattr(s, "projects_dir", None)
        self.config = (self.runtime.config.get("tools") or {}).get("schedule", {}) or {}
        from runtime import paths as _paths
        self.schedules = ScheduleStore(
            self.config.get("store", str(_paths.DATA / "schedules.json")))
        self.runs = ScheduleRunStore(_sched_runs_db(s, s.chats))
        self.work = WorkStore(self.runs.db_path)
        self.orphan_logged: set[str] = set()
        s.work_store = self.work
        s.scheduler_tick = self.tick
        s.dispatch_scheduled_work = self.dispatch_attempts
        s.deliver_scheduled_effects = self.deliver_effects

    def scheduled_chat_id(self, owner: str) -> str | None:
        for chat in self.chats.list(owner):
            if chat["title"] == "⏰ Scheduled runs":
                return chat["id"]
        return None

    def validate_work(self, work: dict) -> str | None:
        """Stored work grants no authority: recheck all launch-time gates."""
        entry = work["item"]["payload"]
        owner = entry.get("owner")
        sid = str(entry.get("id") or "?")
        if owner and self.users.get(owner) is None:
            if sid not in self.orphan_logged:
                self.orphan_logged.add(sid)
                print(f"[scheduler] skipping {sid}: owner '{owner}' no longer "
                      "exists — delete the schedule entry")
            return "owner no longer exists"
        current = next((e for e in self.schedules.list()
                        if e.get("id") == entry.get("id")), None)
        if current is None:
            return "schedule deleted before launch"
        if current.get("manual_disabled"):
            return "schedule disabled before launch"
        if not current.get("enabled"):
            consumed_once = (
                current.get("kind") == "once"
                and current.get("last_occurrence") is not None
                and float(current["last_occurrence"])
                == float(work["attempt"]["scheduled_for"]))
            if not consumed_once:
                return "schedule disabled before launch"
        planted = entry.get("created_by")
        if (not (planted and planted != owner)
                and _schedule_owner_revoked(self.users, owner)):
            return "schedules revoked before launch"
        return None

    async def deliver_effects(self) -> None:
        """Drain due chat effects; the sink is run_id-idempotent."""
        while (effect := self.work.next_effect()) is not None:
            payload = effect["payload"]
            try:
                self.chats.append_scheduled_turn(
                    payload.get("owner"), payload.get("prompt", ""), {
                        "user_message": f"⏰ {payload.get('prompt', '')}",
                        "answer": payload.get("answer", ""),
                        "run_id": payload.get("run_id"),
                        "status": payload.get("status"),
                        "events": []})
                self.work.mark_effect_applied(effect["id"])
            except Exception as exc:
                self.work.mark_effect_failed(effect["id"], str(exc))
                print(f"[scheduler] chat delivery {effect['id'][:8]} failed: {exc}")
                return

    async def execute_work(self, work: dict) -> None:
        entry = work["item"]["payload"]
        attempt = work["attempt"]
        sid = str(entry.get("id") or "?")
        if problem := self.validate_work(work):
            self.work.finish_attempt(
                attempt["id"], result_status="error", answer="", error=problem,
                duration_s=None, tokens=None, deliver=False)
            self.schedules.set_status(sid, "error")
            return
        owner = entry.get("owner")
        prompt = entry.get("prompt", "")
        run_id = attempt["run_id"]
        t0 = time.time()
        chat_id = self.scheduled_chat_id(owner)
        wr, fire_note = _sched_work_root(entry, owner, chat_id,
                                         self.projects_dir, self.scratch_root)
        is_admin = bool((self.users.get(owner) or {}).get("is_admin")) if owner else False

        async def on_event(event: dict) -> None:
            await self.bus.publish(run_id, event)

        # Launched work is interrupted on recovery, never replayed automatically.
        if not self.work.mark_launched(attempt["id"]):
            return
        task = asyncio.create_task(self.runtime.run(
            "This is a SCHEDULED run the user set up earlier. Do what the task "
            "says, then give a short, direct report — it lands in the user's "
            "'⏰ Scheduled runs' chat.\n\nTASK:\n" + prompt,
            run_id=run_id, on_event=on_event, owner=owner,
            is_admin=is_admin,
            work_root=str(wr) if wr else None,
            scratch_key=run_id,
            auto_confirm=bool(self.config.get("auto_confirm", True)) and is_admin,
            confirm_provider=_DenyConfirm(),
            budget_overrides=self.config.get("budget") or None,
            disabled_tools=(set(self.users.get_global_disabled_tools())
                            | _schedule_tool_gate(self.users, owner or "", is_admin)),
            stream=True))
        self.tasks[run_id] = task
        self.run_owner[run_id] = owner
        try:
            out = await task
        except Exception as exc:
            print(f"[scheduler] run {sid} failed: {exc}")
            self.work.finish_attempt(
                attempt["id"], result_status="error", answer="",
                error=f"run failed: {exc}", duration_s=round(time.time() - t0, 1),
                tokens=None, deliver=False)
            self.schedules.set_status(sid, "error")
            return
        finally:
            self.tasks.pop(run_id, None)
            self.run_owner.pop(run_id, None)

            async def _forget() -> None:
                from web import server as _srv
                await asyncio.sleep(_srv._FORGET_AFTER_S)
                self.bus.forget(run_id)
            spawn_background(_forget(), name=f"bus-forget-{run_id[:8]}")
        status, answer, tokens = _run_outcome(out)
        self.work.finish_attempt(
            attempt["id"], result_status=status, answer=answer,
            excerpt=fire_note + answer, duration_s=round(time.time() - t0, 1),
            tokens=tokens)
        self.schedules.set_status(sid, status)
        await self.deliver_effects()

    async def dispatch_attempts(self) -> None:
        while (work := self.work.claim_next()) is not None:
            try:
                await self.execute_work(work)
            except Exception as exc:
                print(f"[scheduler] work {work['item']['id'][:8]} failed: {exc}")

    async def tick(self) -> None:
        await self.deliver_effects()         # also retries after backoff
        due = self.schedules.due()[: int(self.config.get("max_per_tick", 2))]
        for entry in _drop_revoked(self.runs, self.schedules, self.users, due):
            sid = str(entry.get("id"))
            occurrence = float(entry["next_fire"])
            queued = self.work.enqueue_occurrence(entry, occurrence)
            if queued["outcome"] == "active":
                continue                    # durable no-overlap guard
            key = f"schedule:{sid}:{occurrence}"
            if not self.schedules.mark_fired(sid, expected_next_fire=occurrence):
                self.work.cancel_occurrence(key)
                continue
            spawn_background(self.dispatch_attempts(), name=f"scheduled-{sid[:8]}")

    async def start(self) -> None:
        self.work.recover()
        spawn_background(self.deliver_effects(), name="scheduled-outbox")
        if not self.config.get("enabled", True):
            return
        spawn_background(self.dispatch_attempts(), name="scheduled-recovery")

        async def loop() -> None:
            while True:
                await asyncio.sleep(int(self.config.get("tick_s", 30)))
                try:
                    await self.tick()
                except Exception as exc:
                    print(f"[scheduler] tick failed: {exc}")

        # Hold a reference — asyncio only weak-refs tasks.
        self.s.prompt_sched_task = asyncio.create_task(loop())
        print(f"[scheduler] enabled (tick {int(self.config.get('tick_s', 30))}s, "
              f"store {self.schedules.path})")


def register(s) -> None:
    runner = ScheduledRunner(s)
    s.startup_hooks.append(runner.start)
