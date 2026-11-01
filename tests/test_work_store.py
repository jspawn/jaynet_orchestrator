"""Durable scheduled work: occurrence dedupe, claims, outbox, and recovery."""
import sqlite3
import threading

from runtime.work_store import WorkStore
from web.store import ScheduleRunStore


def _store(tmp_path):
    path = str(tmp_path / "chats.db")
    ScheduleRunStore(path)
    return WorkStore(path)


def _entry(sid="s1", owner="alice", fire=100.0, prompt="report"):
    return {"id": sid, "owner": owner, "prompt": prompt, "kind": "every",
            "project_id": "p1", "next_fire": fire, "enabled": True}


def _one_work(tmp_path):
    st = _store(tmp_path)
    assert st.enqueue_occurrence(_entry())["outcome"] == "enqueued"
    claimed = st.claim_next()
    assert claimed is not None
    return st, claimed


def test_occurrence_dedupe_and_active_schedule_guard(tmp_path):
    st = _store(tmp_path)
    first = st.enqueue_occurrence(_entry())
    assert first["outcome"] == "enqueued"
    again = st.enqueue_occurrence(_entry())
    assert again["outcome"] == "existing"
    assert again["item_id"] == first["item_id"]
    later = st.enqueue_occurrence(_entry(fire=200.0))
    assert later["outcome"] == "active"
    with sqlite3.connect(st.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM work_attempts").fetchone()[0] == 1


def test_claim_is_atomic_across_connections(tmp_path):
    st = _store(tmp_path)
    st.enqueue_occurrence(_entry())
    other = WorkStore(st.db_path)
    claimed = []

    def claim(store):
        claimed.append(store.claim_next())

    threads = [threading.Thread(target=claim, args=(s,)) for s in (st, other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(row is not None for row in claimed) == 1
    work = next(row for row in claimed if row)
    assert work["item"]["payload"]["prompt"] == "report"
    assert work["attempt"]["status"] == "running"
    assert work["attempt"]["run_id"]
    assert st.claim_next() is None


def test_completion_history_and_delivery_are_transactional(tmp_path):
    st, work = _one_work(tmp_path)
    attempt = work["attempt"]
    assert st.mark_launched(attempt["id"])
    assert not st.mark_launched(attempt["id"])
    assert st.finish_attempt(
        attempt["id"], result_status="ok", answer="full answer",
        excerpt="short answer", duration_s=1.25, tokens=42)
    effect = st.next_effect()
    assert effect["dedupe_key"] == f"scheduled-chat:{attempt['run_id']}"
    assert effect["payload"]["answer"] == "full answer"
    rows = st.history("s1")
    assert rows[0]["status"] == "ok"
    assert rows[0]["item_id"] == work["item"]["id"]
    assert rows[0]["attempt_id"] == attempt["id"]
    assert rows[0]["scheduled_for"] == 100.0
    assert rows[0]["delivery_status"] == "pending"
    assert st.mark_effect_applied(effect["id"])
    assert st.next_effect() is None
    assert st.history("s1")[0]["work_status"] == "completed"
    assert st.history("s1")[0]["delivery_status"] == "applied"


def test_failed_run_can_skip_chat_delivery(tmp_path):
    st, work = _one_work(tmp_path)
    st.mark_launched(work["attempt"]["id"])
    st.finish_attempt(work["attempt"]["id"], result_status="error", answer="",
                      error="model exploded", duration_s=None, tokens=None,
                      deliver=False)
    assert st.next_effect() is None
    row = st.history("s1")[0]
    assert row["work_status"] == "failed"
    assert row["attempt_status"] == "failed"
    assert row["work_error"] == "model exploded"


def test_effect_failure_uses_backoff(tmp_path):
    st, work = _one_work(tmp_path)
    st.finish_attempt(work["attempt"]["id"], result_status="ok", answer="a",
                      duration_s=1, tokens=1)
    effect = st.next_effect(now=100)
    st.mark_effect_failed(effect["id"], "chat unavailable", now=100)
    assert st.next_effect(now=104) is None
    retry = st.next_effect(now=105)
    assert retry["attempt_count"] == 1
    assert retry["last_error"] == "chat unavailable"


def test_cancel_queued_but_not_running(tmp_path):
    st, running = _one_work(tmp_path)
    st.enqueue_occurrence(_entry(sid="s2"))
    assert st.cancel_source("s1", "deleted") == 0
    assert st.cancel_source("s2", "disabled") == 1
    row = st.history("s2")[0]
    assert row["status"] == "cancelled"
    assert row["work_status"] == "cancelled"
    assert running["attempt"]["status"] == "running"


def test_recover_requeues_only_prelaunch_claims(tmp_path):
    st, work = _one_work(tmp_path)
    assert st.recover()["requeued"] == 1
    again = st.claim_next()
    assert again["attempt"]["id"] == work["attempt"]["id"]
    st.mark_launched(again["attempt"]["id"])
    assert st.recover()["interrupted"] == 1
    row = st.history("s1")[0]
    assert row["status"] == "interrupted"
    assert row["work_status"] == "interrupted"
    assert row["attempt_status"] == "interrupted"
    assert "before retry" in row["work_error"]
    assert st.claim_next() is None                 # never replay launched work


def test_recover_finishes_applied_delivery_and_keeps_pending(tmp_path):
    st, first = _one_work(tmp_path)
    st.finish_attempt(first["attempt"]["id"], result_status="ok", answer="a",
                      duration_s=1, tokens=1)
    effect = st.next_effect()
    st.mark_effect_applied(effect["id"])
    # Simulate a crash after chat apply but before item terminal bookkeeping.
    with sqlite3.connect(st.db_path) as conn:
        conn.execute("UPDATE work_items SET status='delivering'")
    st.enqueue_occurrence(_entry(sid="s2"))
    second = st.claim_next()
    st.finish_attempt(second["attempt"]["id"], result_status="ok", answer="b",
                      duration_s=1, tokens=1)
    counts = st.recover()
    assert counts["completed_delivery"] == 1
    assert st.history("s1")[0]["work_status"] == "completed"
    assert st.history("s2")[0]["delivery_status"] == "pending"


def test_pruning_keeps_active_and_interrupted_work(tmp_path):
    st = _store(tmp_path)
    for i in range(55):
        st.enqueue_occurrence(_entry(fire=float(i)))
        work = st.claim_next()
        st.finish_attempt(work["attempt"]["id"], result_status="ok", answer="x",
                          duration_s=0.1, tokens=1, deliver=False)
    st.enqueue_occurrence(_entry(sid="active"))
    st.enqueue_occurrence(_entry(sid="interrupted"))
    claimed = None
    while True:
        candidate = st.claim_next()
        if candidate["item"]["source_id"] == "interrupted":
            claimed = candidate
            break
    st.mark_launched(claimed["attempt"]["id"])
    st.recover()
    with sqlite3.connect(st.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schedule_runs WHERE schedule_id='s1'").fetchone()[0] == 50
        assert conn.execute("SELECT COUNT(*) FROM work_items WHERE source_id='s1'").fetchone()[0] == 50
        statuses = dict(conn.execute(
            "SELECT source_id,status FROM work_items WHERE source_id IN "
            "('active','interrupted')").fetchall())
    assert statuses == {"active": "queued", "interrupted": "interrupted"}


def test_owner_cleanup_removes_work_effects_attempts_and_history(tmp_path):
    st, work = _one_work(tmp_path)
    st.finish_attempt(work["attempt"]["id"], result_status="ok", answer="a",
                      duration_s=1, tokens=1)
    st.enqueue_occurrence(_entry(sid="bob", owner="bob"))
    assert st.remove_owner("alice") == 1
    assert st.history("s1") == []
    assert st.next_effect() is None
    with sqlite3.connect(st.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_attempts a "
                            "JOIN work_items i ON i.id=a.item_id "
                            "WHERE i.owner='alice'").fetchone()[0] == 0
        assert conn.execute("SELECT source_id FROM work_items").fetchall() == [("bob",)]
