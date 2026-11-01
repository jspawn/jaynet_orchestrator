"""Observe-mode trigger events and immutable deterministic decisions."""
import sqlite3

from runtime.trigger_store import TriggerStore, mandate_hash


def _entry(sid="watch1", owner="alice"):
    return {"id": sid, "owner": owner, "source": "arxiv", "project_id": "p1",
            "rule_version": "arxiv-new-version-v1", "prompt_version": 1,
            "tool_scope": ["arxiv.search", "arxiv.get"],
            "budget": {"max_iterations": 12}, "prompt": "summarize",
            "query": "cat:cs.LG"}


def _paper(pid="2301.12345v1", title="A paper"):
    return {"id": pid, "title": title, "authors": ["Ada"],
            "published": "2023-01-01", "updated": "2023-01-02",
            "summary": "abstract", "primary_category": "cs.LG",
            "categories": ["cs.LG"], "comment": "", "doi": "",
            "pdf_url": "https://example/pdf", "abs_url": "https://example/abs"}


def test_first_poll_baselines_and_duplicate_replay_is_noop(tmp_path):
    store = TriggerStore(str(tmp_path / "chats.db"))
    entry = _entry()
    result = store.record_poll(entry, [_paper(), _paper("2301.99999v1")], 100)
    assert result == {"recorded": 2, "duplicates": 0, "outcome": "baseline"}
    again = store.record_poll(entry, [_paper(), _paper("2301.99999v1")], 200)
    assert again == {"recorded": 0, "duplicates": 2, "outcome": None}
    rows = store.history(entry["id"])
    assert len(rows) == 2
    assert {r["outcome"] for r in rows} == {"baseline"}
    assert rows[0]["mandate_hash"] == mandate_hash(entry)
    assert rows[0]["rule_version"] == "arxiv-new-version-v1"
    assert rows[0]["prompt_version"] == 1
    assert rows[0]["scheduled_for"] == 100
    assert rows[0]["payload"]["title"] == "A paper"
    assert "prompt" not in rows[0]["payload"]


def test_new_version_would_accept_and_empty_first_poll_does_not_baseline(tmp_path):
    store = TriggerStore(str(tmp_path / "chats.db"))
    entry = _entry()
    assert store.record_poll(entry, [], 100)["recorded"] == 0
    assert store.record_poll(entry, [_paper()], 200)["outcome"] == "baseline"
    changed = store.record_poll(entry, [_paper("2301.12345v2")], 300)
    assert changed == {"recorded": 1, "duplicates": 0, "outcome": "would_accept"}
    assert store.history(entry["id"])[0]["external_id"] == "2301.12345v2"
    assert store.history(entry["id"])[0]["outcome"] == "would_accept"


def test_atomic_batch_and_bounded_history(tmp_path):
    store = TriggerStore(str(tmp_path / "chats.db"))
    entry = _entry()
    try:
        store.record_poll(entry, [_paper(), {"title": "missing id"}], 100)
    except ValueError as exc:
        assert "no id" in str(exc)
    else:
        raise AssertionError("invalid source record must fail the batch")
    assert store.history(entry["id"]) == []
    store.record_poll(entry, [_paper(f"2301.{i:05d}v1") for i in range(55)], 100)
    assert len(store.history(entry["id"], limit=500)) == 50


def test_owner_cleanup_cascades_decisions(tmp_path):
    store = TriggerStore(str(tmp_path / "chats.db"))
    store.record_poll(_entry(), [_paper()], 100)
    store.record_poll(_entry("other", "bob"), [_paper()], 100)
    assert store.remove_owner("alice") == 1
    assert store.history("watch1") == []
    assert len(store.history("other")) == 1
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trigger_decisions").fetchone()[0] == 1
