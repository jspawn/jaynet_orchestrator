"""Tests for the context/latency optimizations and code.delegate."""
import json

from conftest import run
from runtime.loop import _compact_messages
from runtime.tool_base import ToolContext
from tools.code.run import CodeRun
from tools.code.delegate import CodeDelegate


# ---------- transcript compaction ----------

def _mk_msgs():
    msgs = [{"role": "system", "content": "S" * 100}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "fs.read",
                     "content": json.dumps({"status": "ok", "result": "X" * 5000})})
    return msgs


def test_compaction_disabled_is_noop():
    msgs = _mk_msgs()
    before = [m["content"] for m in msgs]
    assert _compact_messages(msgs, {"enabled": False}) == 0
    assert [m["content"] for m in msgs] == before


def test_compaction_shrinks_old_keeps_recent():
    msgs = _mk_msgs()
    n = _compact_messages(msgs, {"enabled": True, "max_result_chars": 2000, "keep_last": 3})
    assert n == 3  # 6 tool msgs, last 3 protected
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert all('"__compacted__"' in m["content"] for m in tool_msgs[:3])
    assert all('"__compacted__"' not in m["content"] for m in tool_msgs[3:])
    # system/assistant untouched
    assert msgs[0]["content"] == "S" * 100


def test_compaction_idempotent():
    msgs = _mk_msgs()
    cfg = {"enabled": True, "max_result_chars": 2000, "keep_last": 3}
    _compact_messages(msgs, cfg)
    assert _compact_messages(msgs, cfg) == 0  # nothing left to compact


def test_compaction_preserves_message_count():
    msgs = _mk_msgs()
    before = len(msgs)
    _compact_messages(msgs, {"enabled": True, "max_result_chars": 2000, "keep_last": 1})
    assert len(msgs) == before  # indices (and privacy taint) stay valid


# ---------- dynamic confirmation ----------

def test_code_run_gated_only_when_sandbox_disabled(project, monkeypatch):
    tool = CodeRun()
    base = {"tools": {"fs": {"allowed_roots": [str(project)]}, "code": {"run": {}}}}
    # default (sandbox_prefix unset -> firejail active and present): not gated
    monkeypatch.setattr("shutil.which", lambda b: f"/usr/bin/{b}")
    ctx_active = ToolContext(request_id="t", config=base, budget=None)
    assert tool.needs_confirmation({"command": "ls"}, ctx_active) is False
    # sandbox explicitly disabled ([]) -> gated
    base2 = {"tools": {"fs": {"allowed_roots": [str(project)]},
                       "code": {"run": {"sandbox_prefix": []}}}}
    ctx_off = ToolContext(request_id="t", config=base2, budget=None)
    assert tool.needs_confirmation({"command": "ls"}, ctx_off) is True


def test_code_run_gated_when_sandbox_binary_missing(project, monkeypatch):
    # firejail not on PATH: the command would run bare, so the approval gate
    # must engage instead of silently degrading (audit H1).
    monkeypatch.setattr("shutil.which", lambda b: None)
    tool = CodeRun()
    base = {"tools": {"fs": {"allowed_roots": [str(project)]}, "code": {"run": {}}}}
    ctx = ToolContext(request_id="t", config=base, budget=None)
    assert tool.needs_confirmation({"command": "ls"}, ctx) is True
    # a custom prefix whose binary is present stays ungated
    monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/bwrap" if b == "bwrap" else None)
    base2 = {"tools": {"fs": {"allowed_roots": [str(project)]},
                       "code": {"run": {"sandbox_prefix": ["bwrap", "--unshare-net"]}}}}
    ctx2 = ToolContext(request_id="t", config=base2, budget=None)
    assert tool.needs_confirmation({"command": "ls"}, ctx2) is False


# ---------- code.delegate ----------

def test_delegate_uses_configured_coder_and_tools(project):
    captured = {}

    async def fake_spawn(task, tools=None, model=None, name=None, budget=None, verify=None,
                     todos_sync=False, work_root_path=None):
        captured.update(task=task, tools=tools, model=model, name=name)
        return {"status": "ok", "answer": "done", "run_id": "sub1", "budget": {}}

    cfg = {"tools": {"code": {"delegate": {"model": "qwen_coder"}}}}
    ctx = ToolContext(request_id="t", config=cfg, budget=None, spawn=fake_spawn)
    r = run(CodeDelegate().execute({"task": "fix the parser in app.py; tests must pass"}, ctx))
    assert r.status == "ok"
    assert captured["model"] == "qwen_coder" and captured["name"] == "coder"
    assert "code.run" in captured["tools"] and "git.commit" in captured["tools"]


def test_delegate_warns_without_coder(project):
    async def fake_spawn(task, tools=None, model=None, name=None, budget=None, verify=None,
                     todos_sync=False, work_root_path=None):
        return {"status": "ok", "answer": "done", "run_id": "s", "budget": {}}
    ctx = ToolContext(request_id="t", config={"tools": {}}, budget=None, spawn=fake_spawn)
    r = run(CodeDelegate().execute({"task": "do a thing"}, ctx))
    assert r.status == "ok" and "no coder alias configured" in r.result["note"]


def test_delegate_requires_spawn(project):
    ctx = ToolContext(request_id="t", config={"tools": {}}, budget=None, spawn=None)
    r = run(CodeDelegate().execute({"task": "x"}, ctx))
    assert r.status == "error" and "sub-agents are not available" in r.error
