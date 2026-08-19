"""Audit robustness batch: B4 (ProcessManager spawn-error retries count toward
max_restarts) and D3 (one malformed schedules.json entry doesn't stall due())."""
import asyncio

from runtime.process_manager import ProcessManager
from runtime.scheduler import ScheduleStore


def test_spawn_errors_count_toward_max_restarts(tmp_path):
    pm = ProcessManager()
    pm.add("bad", "true", cwd=str(tmp_path / "no-such-dir"), restart_delay=0.01)
    mp = pm._procs["bad"]
    mp.max_restarts = 2
    asyncio.run(pm._run_loop(mp))
    assert mp.restarts == 3   # capped: 3 failed attempts, then give up
    assert any("giving up" in line for line in mp.log)


def test_due_quarantines_malformed_entry(tmp_path):
    store = tmp_path / "s.json"
    store.write_text(
        '[{"id": "bad", "enabled": true, "next_fire": "not-a-number"},'
        ' {"id": "ok", "enabled": true, "next_fire": 1}]')
    due = ScheduleStore(store).due(now=1000)
    assert [e["id"] for e in due] == ["ok"]
