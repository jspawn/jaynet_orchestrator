"""Atomic scheduled-result chat delivery (WorkStore outbox sink)."""
import threading

from web.store import ChatStore


def _turn(run_id, answer):
    return {"answer": answer, "run_id": run_id, "status": "ok", "events": []}


def test_append_is_idempotent_and_preserves_existing_turns(tmp_path):
    store = ChatStore(str(tmp_path / "chats.db"))
    store.upsert("other", "Other", [{"user_message": "keep", "answer": "me"}],
                 owner="alice")
    first = store.append_scheduled_turn("alice", "report", _turn("r1", "one"))
    duplicate = store.append_scheduled_turn("alice", "report", _turn("r1", "changed"))
    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["id"] == first["id"] and duplicate["turns"] == 1
    chat = store.get(first["id"], "alice")
    assert chat["title"] == "⏰ Scheduled runs"
    assert [(t["run_id"], t["answer"]) for t in chat["turns"]] == [("r1", "one")]
    assert chat["turns"][0]["user_message"] == "⏰ report"
    assert len(store.get("other", "alice")["turns"]) == 1


def test_concurrent_completions_do_not_lose_turns(tmp_path):
    store = ChatStore(str(tmp_path / "chats.db"))
    errors = []

    def append(i):
        try:
            ChatStore(store.db_path).append_scheduled_turn(
                "alice", f"task {i}", _turn(f"r{i}", f"answer {i}"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    chats = store.list("alice")
    assert len(chats) == 1 and chats[0]["turns"] == 12
    turns = store.get(chats[0]["id"], "alice")["turns"]
    assert {t["run_id"] for t in turns} == {f"r{i}" for i in range(12)}
    assert [t["idx"] for t in turns] == list(range(12))
