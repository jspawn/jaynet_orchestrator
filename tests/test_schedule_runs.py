"""ScheduleRunStore (web/store.py): firing-history rows for the prompt
scheduler — insert, newest-first history with filters, the 50-row per-schedule
cap, and owner deletion."""
from web.store import ScheduleRunStore


def _store(tmp_path):
    return ScheduleRunStore(str(tmp_path / "chats.db"))


def test_add_and_history_newest_first(tmp_path):
    st = _store(tmp_path)
    st.add("s1", "alice", "r1", "ok", 1.5, "did the thing", 123)
    st.add("s1", "alice", "r2", "error", 0.25, "boom", None)
    rows = st.history(schedule_id="s1")
    assert [r["run_id"] for r in rows] == ["r2", "r1"]
    r1 = rows[1]
    assert (r1["schedule_id"], r1["owner"], r1["status"]) == ("s1", "alice", "ok")
    assert r1["duration_s"] == 1.5 and r1["excerpt"] == "did the thing"
    assert r1["tokens"] == 123 and r1["fired_at"] > 0
    assert rows[0]["tokens"] is None
    # persists across instances (same file as the chats store)
    assert len(_store(tmp_path).history()) == 2


def test_history_filters_and_limit(tmp_path):
    st = _store(tmp_path)
    st.add("s1", "alice", "r1", "ok", 1.0, "a", 1)
    st.add("s2", "bob", "r2", "ok", 1.0, "b", 2)
    st.add("s1", "alice", "r3", "error", 1.0, "c", 3)
    assert [r["run_id"] for r in st.history(owner="alice")] == ["r3", "r1"]
    assert [r["run_id"] for r in st.history(schedule_id="s1", owner="alice")] \
        == ["r3", "r1"]
    assert len(st.history(limit=2)) == 2
    assert len(st.history(schedule_id="nope")) == 0


def test_prunes_to_latest_50_per_schedule(tmp_path):
    st = _store(tmp_path)
    for i in range(55):
        st.add("s1", "alice", f"r{i}", "ok", 1.0, f"run {i}", i)
    st.add("s2", "alice", "other", "ok", 1.0, "other", 0)
    rows = st.history(schedule_id="s1", limit=200)
    assert len(rows) == 50
    assert rows[0]["run_id"] == "r54"        # newest kept
    assert "r0" not in {r["run_id"] for r in rows}   # oldest pruned
    assert len(st.history(schedule_id="s2")) == 1    # other schedule untouched


def test_remove_owner(tmp_path):
    st = _store(tmp_path)
    st.add("s1", "eve", "r1", "ok", 1.0, "x", 1)
    st.add("s2", "eve", "r2", "ok", 1.0, "y", 1)
    st.add("s3", "bob", "r3", "ok", 1.0, "z", 1)
    assert st.remove_owner("eve") == 2
    assert [r["owner"] for r in st.history()] == ["bob"]
    assert st.remove_owner("eve") == 0
