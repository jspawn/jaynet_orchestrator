"""Readiness audit QA-4/DB-1: every store must run WAL + a busy timeout —
eval.db and presets.db quietly missed the conversion, and a reader opening
the Eval tab mid-suite could crash a paid result write with 'database is
locked'. One WAL assertion for eight databases was how that slipped through.
"""
import sqlite3
import threading
import time

from runtime.cloud_store import CloudStore
from runtime.eval_store import EvalStore
from runtime.preset_store import PresetStore


def _pragma(db, name):
    return sqlite3.connect(db).execute(f"PRAGMA {name}").fetchone()[0]


def test_eval_store_wal_and_busy_timeout(tmp_path):
    db = tmp_path / "eval.db"
    store = EvalStore(db)
    store.close()
    assert str(_pragma(db, "journal_mode")).lower() == "wal"
    assert int(_pragma(db, "busy_timeout")) > 0


def test_preset_store_wal_and_busy_timeout(tmp_path):
    db = tmp_path / "presets.db"
    PresetStore(str(db)).ensure()
    assert str(_pragma(db, "journal_mode")).lower() == "wal"
    assert int(_pragma(db, "busy_timeout")) > 0


def test_cloud_store_wal_and_busy_timeout(tmp_path):
    db = tmp_path / "presets.db"
    CloudStore(str(db)).ensure()
    assert str(_pragma(db, "journal_mode")).lower() == "wal"
    assert int(_pragma(db, "busy_timeout")) > 0


def test_eval_store_shared_process_lock(tmp_path):
    """The serialising lock is module-level: two EvalStore INSTANCES (what
    the routes construct per request) serialise against each other."""
    a = EvalStore(tmp_path / "eval.db")
    b = EvalStore(tmp_path / "eval.db")
    assert a._lock is b._lock
    a.close()
    b.close()


def test_eval_write_survives_concurrent_reader(tmp_path):
    """DB-1's live failure: a reader (Eval tab / reflect loop) on a second
    connection while record_result writes must not raise 'database is
    locked' — WAL readers don't exclude the writer at all."""
    db = tmp_path / "eval.db"
    store = EvalStore(db)
    errors = []

    def reader():
        try:
            conn = sqlite3.connect(db, timeout=10)
            for _ in range(50):
                conn.execute("SELECT COUNT(*) FROM results").fetchone()
                time.sleep(0.002)
            conn.close()
        except Exception as e:  # noqa: BLE001 — surfaced via assertion
            errors.append(e)

    t = threading.Thread(target=reader)
    t.start()
    for i in range(25):
        store.record_result(test_id=f"case-{i}", passed=True, score=1.0,
                            judge_notes="", judge_model="j", cost_usd=0.0,
                            tokens=0, elapsed_s=0.1, status="ok",
                            run_ids=[], transcript=[])
    t.join()
    store.close()
    assert not errors
