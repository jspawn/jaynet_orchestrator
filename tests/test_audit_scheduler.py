"""Audit regressions for the scheduler.

B9: ScheduleStore serializes load→mutate→save across instances (module-level
lock) and a crash mid-write (leftover .tmp) never corrupts the main file.
B10: the web tick fires due entries as background tasks (a blocking run does
not stall later entries) and never re-fires an entry whose previous run is
still in flight. runtime.run is mocked — no real model.
"""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

from conftest import run

from runtime.scheduler import ScheduleStore
from web import routes_procs

# ---- B9: store serialization + atomic writes ----------------------------------

def test_concurrent_adds_from_threads_all_survive(tmp_path):
    path = str(tmp_path / "s.json")

    def worker(n):
        store = ScheduleStore(path)              # per-call instance, like the tools
        for i in range(20):
            store.add({"owner": "t", "prompt": f"{n}-{i}", "kind": "once",
                       "next_fire": time.time() + 3600})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ScheduleStore(path).list()) == 80


def test_add_and_mark_fired_interleave_without_loss(tmp_path):
    # The web tick (mark_fired) and schedule.* tools (add) hit the same file.
    path = str(tmp_path / "s.json")
    s = ScheduleStore(path)
    e = s.add({"owner": "a", "prompt": "x", "kind": "once",
               "next_fire": time.time() - 1})
    errors = []

    def adder():
        try:
            for i in range(20):
                ScheduleStore(path).add({"owner": "b", "prompt": f"y{i}",
                                         "kind": "once",
                                         "next_fire": time.time() + 3600})
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=adder)
    t.start()
    for _ in range(20):
        s.mark_fired(e["id"])
    t.join()
    assert not errors
    assert len(ScheduleStore(path).list()) == 21


def test_leftover_tmp_does_not_corrupt_main(tmp_path):
    path = tmp_path / "s.json"
    s = ScheduleStore(str(path))
    s.add({"owner": "a", "prompt": "x", "kind": "once", "next_fire": 1.0})
    # Simulate a crash mid-write: a truncated tmp sibling is left behind.
    path.with_suffix(".tmp").write_text('{"partial"')
    assert [e["prompt"] for e in ScheduleStore(str(path)).list()] == ["x"]
    # The next save overwrites the stale tmp and replaces the main file.
    s.add({"owner": "a", "prompt": "y", "kind": "once", "next_fire": 1.0})
    assert len(ScheduleStore(str(path)).list()) == 2


# ---- B10: tick fire-and-track + in-flight guard --------------------------------

class _App:
    """Minimal FastAPI stand-in: captures decorators, ignores route paths.
    Startup/shutdown hooks collect on the state namespace's hook lists
    (lifespan pattern) — no on_event shim needed here."""

    def get(self, _path):
        return lambda fn: fn

    def post(self, _path):
        return lambda fn: fn


class _Chats:
    def __init__(self):
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


def _wired(tmp_path, run_impl, max_per_tick=10):
    """register() against fakes; returns (state, store). The tick is driven
    directly via state.scheduler_tick — the 30s loop never runs."""
    store_path = tmp_path / "schedules.json"
    s = SimpleNamespace(
        runtime=SimpleNamespace(
            config={"tools": {"schedule": {"store": str(store_path),
                                           "max_per_tick": max_per_tick}}},
            run=run_impl),
        bus=_Bus(), tasks={}, run_owner={}, users=None, chats=_Chats(),
        _scratch_root=lambda owner, chat_id: None,
        goal_kick=lambda u: None,
        startup_hooks=[], shutdown_hooks=[],
    )
    routes_procs.register(_App(), s)
    return s, ScheduleStore(str(store_path))


def test_blocking_run_does_not_stall_other_due_entries(tmp_path):
    async def main():
        release = asyncio.Event()
        started = []

        async def fake_run(msg, **kw):
            prompt = msg.split("TASK:\n")[-1]
            started.append(prompt)
            if prompt == "slow":
                await release.wait()             # "runs forever" until released
            return {"answer": "done", "status": "done"}

        s, store = _wired(tmp_path, fake_run)
        store.add({"owner": "a", "prompt": "slow", "kind": "once",
                   "next_fire": time.time() - 1})
        store.add({"owner": "a", "prompt": "fast", "kind": "once",
                   "next_fire": time.time() - 1})

        await s.scheduler_tick()                 # must return, not await 'slow'
        for _ in range(200):
            if len(started) == 2:
                break
            await asyncio.sleep(0.01)
        assert sorted(started) == ["fast", "slow"]   # fast fired despite slow
        # 'fast' already completed and was recorded in the Scheduled runs chat
        assert any(t["turns"][-1]["user_message"] == "⏰ fast"
                   for t in s.chats.upserts)
        release.set()
        for _ in range(200):
            if len(s.chats.upserts) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(s.chats.upserts) == 2

    run(main())


def test_in_flight_entry_is_not_refired(tmp_path):
    async def main():
        release = asyncio.Event()
        calls = []

        async def fake_run(msg, **kw):
            calls.append(1)
            await release.wait()
            return {"answer": "", "status": "done"}

        s, store = _wired(tmp_path, fake_run)
        store.add({"owner": "a", "prompt": "recur", "kind": "every",
                   "every_s": 60, "next_fire": time.time() - 1})

        await s.scheduler_tick()                 # fires; the run blocks
        await asyncio.sleep(0.05)
        assert len(calls) == 1
        # mark-before-fire preserved: consumed at fire time, not at completion
        entry = store.list()[0]
        assert entry["fire_count"] == 1 and entry["next_fire"] > time.time()

        # The interval elapses while the run is still in flight...
        data = json.loads(store.path.read_text())
        data[0]["next_fire"] = time.time() - 1
        store.path.write_text(json.dumps(data))
        await s.scheduler_tick()                 # ...but the guard skips it
        await asyncio.sleep(0.05)
        assert len(calls) == 1

        release.set()                            # run finishes, guard clears
        await asyncio.sleep(0.05)
        data = json.loads(store.path.read_text())
        data[0]["next_fire"] = time.time() - 1
        store.path.write_text(json.dumps(data))
        await s.scheduler_tick()                 # now it may fire again
        await asyncio.sleep(0.05)
        assert len(calls) == 2
        release.set()
        await asyncio.sleep(0.05)

    run(main())


def test_failing_run_is_logged_and_guard_released(tmp_path, capsys):
    async def main():
        calls = []

        async def boom(msg, **kw):
            calls.append(1)
            raise RuntimeError("model exploded")

        s, store = _wired(tmp_path, boom)
        store.add({"owner": "a", "prompt": "recur", "kind": "every",
                   "every_s": 60, "next_fire": time.time() - 1})

        await s.scheduler_tick()
        await asyncio.sleep(0.05)
        assert len(calls) == 1

        # The failure released the in-flight guard: the entry may fire again.
        data = json.loads(store.path.read_text())
        data[0]["next_fire"] = time.time() - 1
        store.path.write_text(json.dumps(data))
        await s.scheduler_tick()
        await asyncio.sleep(0.05)
        assert len(calls) == 2

    run(main())
    out = capsys.readouterr().out
    assert "[scheduler] run" in out and "model exploded" in out
