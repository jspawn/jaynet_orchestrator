"""schedule.*: time parsing, the JSON store, and the tools (owner scoping,
validation). The web server's tick/fire loop itself is not covered here."""
import asyncio
import time

import pytest

from runtime.scheduler import ScheduleStore, parse_every, parse_when
from runtime.tool_base import ToolContext
from tools.schedule.schedule import ScheduleAdd, ScheduleList, ScheduleRemove


def _ctx(owner="alice", store_path=None):
    cfg = {"tools": {"schedule": {"store": str(store_path)}}} if store_path else {}
    return ToolContext(request_id="t", config=cfg, budget=None, owner=owner)


def _run(tool, args, ctx):
    return asyncio.run(tool.execute(args, ctx))


# ---- parsing ----
def test_parse_every():
    assert parse_every("30m") == 1800
    assert parse_every("2h") == 7200
    assert parse_every("1d") == 86400
    assert parse_every("1w") == 604800
    with pytest.raises(ValueError):
        parse_every("soon")


def test_parse_when_relative_and_iso():
    now = 1_000_000.0
    assert parse_when("+15m", now) == now + 900
    ts = parse_when("2030-01-01T10:00:00+00:00", now)
    assert ts == 1893492000.0
    with pytest.raises(ValueError):
        parse_when("next tuesday-ish", now)


# ---- store ----
def test_store_add_list_remove(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.json"))
    a = s.add({"owner": "alice", "prompt": "p1", "kind": "once",
               "next_fire": time.time() + 100})
    s.add({"owner": "bob", "prompt": "p2", "kind": "once",
           "next_fire": time.time() + 100})
    assert [e["prompt"] for e in s.list("alice")] == ["p1"]
    assert len(s.list()) == 2
    # persists across instances
    assert len(ScheduleStore(str(tmp_path / "s.json")).list()) == 2
    assert s.remove(a["id"], "bob") is False          # not yours
    assert s.remove(a["id"], "alice") is True
    assert len(s.list()) == 1


def test_store_due_and_mark_fired_once(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.json"))
    e = s.add({"owner": "a", "prompt": "x", "kind": "once", "next_fire": time.time() - 1})
    future = s.add({"owner": "a", "prompt": "y", "kind": "once",
                    "next_fire": time.time() + 3600})
    due = s.due()
    assert [d["id"] for d in due] == [e["id"]]
    s.mark_fired(e["id"])
    after = {x["id"]: x for x in s.list()}
    assert after[e["id"]]["enabled"] is False and after[e["id"]]["fire_count"] == 1
    assert after[future["id"]]["enabled"] is True


def test_store_recurring_advances_without_drift(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.json"))
    t0 = time.time() - 10_000                     # scheduled long ago
    e = s.add({"owner": "a", "prompt": "x", "kind": "every", "every_s": 3600,
               "next_fire": t0})
    s.mark_fired(e["id"])
    after = s.list()[0]
    assert after["enabled"] is True
    # anchored on the scheduled time: lands on a future multiple of the interval
    assert after["next_fire"] > time.time()
    assert (after["next_fire"] - t0) % 3600 == 0


def test_store_tolerates_missing_and_corrupt_file(tmp_path):
    s = ScheduleStore(str(tmp_path / "missing.json"))
    assert s.list() == []
    (tmp_path / "bad.json").write_text("{nope")
    assert ScheduleStore(str(tmp_path / "bad.json")).list() == []


# ---- tools ----
def test_add_requires_exactly_one_of_run_at_every(tmp_path):
    ctx = _ctx(store_path=tmp_path / "s.json")
    r = _run(ScheduleAdd(), {"prompt": "x"}, ctx)
    assert r.status == "error" and "exactly one" in r.error
    r = _run(ScheduleAdd(), {"prompt": "x", "run_at": "+1h", "every": "1d"}, ctx)
    assert r.status == "error" and "exactly one" in r.error


def test_add_list_remove_roundtrip(tmp_path):
    store = tmp_path / "s.json"
    ctx = _ctx(store_path=store)
    r = _run(ScheduleAdd(), {"prompt": "check the deploy", "run_at": "+30m"}, ctx)
    assert r.status == "ok" and r.result["kind"] == "once"
    r = _run(ScheduleAdd(), {"prompt": "morning report", "every": "1d"}, ctx)
    assert r.status == "ok" and r.result["kind"] == "every"
    lst = _run(ScheduleList(), {}, ctx).result
    assert lst["count"] == 2
    sid = lst["schedules"][0]["id"]
    assert _run(ScheduleRemove(), {"id": sid}, ctx).status == "ok"
    assert _run(ScheduleList(), {}, ctx).result["count"] == 1


def test_owner_scoping(tmp_path):
    store = tmp_path / "s.json"
    alice, bob = _ctx("alice", store), _ctx("bob", store)
    _run(ScheduleAdd(), {"prompt": "alice's", "run_at": "+1h"}, alice)
    _run(ScheduleAdd(), {"prompt": "bob's", "run_at": "+1h"}, bob)
    assert _run(ScheduleList(), {}, alice).result["count"] == 1
    aid = _run(ScheduleList(), {}, alice).result["schedules"][0]["id"]
    assert _run(ScheduleRemove(), {"id": aid}, bob).status == "error"


def test_add_is_confirmation_gated():
    assert ScheduleAdd().requires_confirmation is True
    assert ScheduleList().requires_confirmation is False
