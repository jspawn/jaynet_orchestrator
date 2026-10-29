"""File-aware and evidence-preserving transcript compaction."""
import asyncio
import json
from types import SimpleNamespace

from runtime.context_compaction import compact_messages
from runtime.loop import AgentRuntime

CFG = {"enabled": True, "max_result_chars": 2000, "keep_last": 1}


def _call(call_id, name, args):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }]}


def _result(call_id, name, content):
    return {"role": "tool", "tool_call_id": call_id, "name": name,
            "content": content}


def _read(call_id, path="src/app.py", content="old", **args):
    return [_call(call_id, "fs.read", {"path": path, **args}),
            _result(call_id, "fs.read", f"# {path}\n{content}")]


def _write(call_id, path="src/app.py", status="ok"):
    content = json.dumps({"status": status,
                          "result": {"path": path} if status == "ok" else None})
    return [_call(call_id, "fs.write", {"path": path, "content": "new"}),
            _result(call_id, "fs.write", content)]


def test_duplicate_read_deduped_against_protected_latest():
    messages = [*_read("r1", content="OLD"), *_read("r2", content="NEW")]
    stats = compact_messages(messages, CFG)
    old, new = messages[1], messages[3]
    stub = json.loads(old["content"])
    assert stub["strategy"] == "stale_read"
    assert stub["reason"] == "newer read of the same file slice"
    assert "OLD" not in old["content"]
    assert new["content"].endswith("NEW")
    assert stats["compacted"] == stats["deduplicated"] == 1


def test_small_duplicate_deduped_and_feature_can_be_disabled():
    messages = [*_read("r1"), *_read("r2")]
    compact_messages(messages, {**CFG, "max_result_chars": 10000})
    assert '"__compacted__"' in messages[1]["content"]

    messages = [*_read("r1"), *_read("r2")]
    stats = compact_messages(messages, {**CFG, "max_result_chars": 10000,
                                       "dedupe_reads": False})
    assert stats["compacted"] == 0
    assert all('"__compacted__"' not in m["content"] for m in messages
               if m["role"] == "tool")


def test_different_file_slices_are_not_duplicates():
    messages = [*_read("r1", start_line=1, end_line=20),
                *_read("r2", start_line=21, end_line=40)]
    stats = compact_messages(messages, {**CFG, "max_result_chars": 10000})
    assert stats["compacted"] == 0


def test_successful_write_invalidates_read_but_failed_write_does_not():
    messages = [*_read("r1"), *_write("w1")]
    compact_messages(messages, {**CFG, "keep_last": 0})
    stub = json.loads(messages[1]["content"])
    assert stub["strategy"] == "stale_read"
    assert stub["reason"] == "later file write"

    messages = [*_read("r1"), *_write("w1", status="error")]
    stats = compact_messages(messages, {**CFG, "max_result_chars": 10000})
    assert stats["compacted"] == 0


def test_pinned_stale_read_stays_verbatim():
    messages = [*_read("r1", content="PINNED"), *_read("r2")]
    stats = compact_messages(messages, CFG, pinned={1})
    assert messages[1]["content"].endswith("PINNED")
    assert stats["compacted"] == 0


def test_code_run_pruning_preserves_evidence_artifacts_and_hint():
    stdout = "BEGIN\n" + ("progress\n" * 500) + "Traceback: final failure\n"
    payload = {"status": "ok", "result": {
        "stdout": stdout, "stderr": "final stderr", "exit_code": 1,
        "ok": False, "written_files": ["report.txt"],
    }}
    messages = [_call("c1", "code.run", {"command": "pytest -q"}),
                _result("c1", "code.run", json.dumps(payload) + "\nCHANGE STRATEGY")]
    stats = compact_messages(messages, {**CFG, "keep_last": 0})
    content = messages[1]["content"]
    assert content.endswith("\nCHANGE STRATEGY")
    compacted, _ = json.JSONDecoder().raw_decode(content)
    result = compacted["result"]
    assert compacted["strategy"] == "log_evidence"
    assert result["command"] == "pytest -q"
    assert result["exit_code"] == 1
    assert result["written_files"] == ["report.txt"]
    assert result["stdout"].startswith("BEGIN")
    assert result["stdout"].endswith("Traceback: final failure\n")
    assert result["stdout_compacted"] is True
    assert "progress\n" * 500 not in result["stdout"]
    assert result["stderr"] == "final stderr"
    assert stats["log_pruned"] == 1
    assert stats["saved_chars"] > 0
    assert stats["estimated_saved_tokens"] == stats["saved_chars"] // 4


def test_generic_log_keeps_head_tail_command_and_exit_code():
    output = "START" + "x" * 4000 + "FINAL ERROR"
    payload = {"status": "ok", "result": {"stdout": output, "exit_code": 2}}
    messages = [_call("c1", "code.check", {"command": "make test"}),
                _result("c1", "code.check", json.dumps(payload))]
    compact_messages(messages, {**CFG, "keep_last": 0})
    stub = json.loads(messages[1]["content"])
    assert stub["command"] == "make test"
    assert stub["exit_code"] == 2
    assert stub["head_tail"].startswith('{"status": "ok"')
    assert "FINAL ERROR" in stub["head_tail"]
    assert stub["head_tail"].endswith('"exit_code": 2}}')


def test_maybe_compact_emits_detailed_telemetry():
    runtime = AgentRuntime.__new__(AgentRuntime)
    rs = SimpleNamespace(messages=[*_read("r1"), *_read("r2")],
                         pinned=set(), budget=SimpleNamespace(iterations=1))
    st = SimpleNamespace(compaction=CFG, compaction_every=1)
    events = []

    async def emit(kind, iteration, data):
        events.append((kind, iteration, data))

    asyncio.run(runtime._maybe_compact(rs, st, emit))
    kind, iteration, data = events[0]
    assert kind == "compaction" and iteration == 1
    assert data["compacted"] == data["deduplicated"] == 1
    assert "saved_chars" in data and "estimated_saved_tokens" in data
