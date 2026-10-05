"""Badge watch: a skill with `requires_badge: true` in frontmatter asks the
model to badge the run (run.badge) after loading. Prompt placement alone
doesn't get small brains to do it (j-space eval history: 12+ of 19 runs
skipped the badge), so the first file edit without a badge carries a
one-shot reminder. The guard owns ONLY the reminder — the state it reads
(rs.badge_watch / rs.badged) is written by the loop's own post-call path
(audit #28 C2), and with loop_guard.jspace_badge_gate on (the default)
the dispatch gate rejects the unbadged edit instead
(tests/test_jspace_badge_gate.py). These tests pin the nudge path (gate
ablated by its registered name) and the frontmatter switch. Real loop,
fake model."""
import asyncio
import json

from runtime.skills import skills_cache_clear
from runtime.tool_base import ToolResult
from tests.test_loop_regressions import _final, _Registry, _runtime, _tc

SKILL_MD = """---
name: j-space
requires_badge: true
description: test badge skill
---
Badge the pass after classifying.
"""


class _ExecStub:
    private = False

    def __init__(self, name, read_only=False):
        self.name = name
        self.read_only = read_only

    def needs_confirmation(self, args, ctx): return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        return ToolResult(status="ok", result={"ok": True})


def _rt(tmp_path, script, skill_md=SKILL_MD):
    d = tmp_path / "skills" / "j-space"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(skill_md)
    skills_cache_clear()
    reg = _Registry([], real={
        "skill.load": _ExecStub("skill.load", read_only=True),
        "run.badge": _ExecStub("run.badge"),
        "fs.write": _ExecStub("fs.write"),
    })
    rt, seen = _runtime(reg, script)
    rt.config["skills"] = {"dir": str(tmp_path / "skills")}
    return rt, seen


def _hints(seen):
    return {m["content"] for msgs in seen for m in msgs
            if m.get("role") == "tool"
            and isinstance(m.get("content"), str)
            and "badge the run" in m["content"]}


def test_first_edit_unbadged_gets_one_reminder(tmp_path):
    """Gate ablated by its registered name (guards_off:
    ["jspace_badge_gate"], audit #28 C2): the one-shot reminder is the
    only consequence of an unbadged edit — the gate's rejection path is
    pinned in tests/test_jspace_badge_gate.py."""
    script = [
        _tc("skill.load", json.dumps({"name": "j-space"})),
        _tc("fs.write", json.dumps({"path": "a.py", "content": "x"})),
        _tc("fs.write", json.dumps({"path": "b.py", "content": "y"})),
        _final("done"),
    ]
    rt, seen = _rt(tmp_path, script)
    out = asyncio.run(rt.run("use the j-space skill for this",
                             guards_off=["jspace_badge_gate"]))
    assert out["status"] == "ok"
    assert len(_hints(seen)) == 1          # one-shot, not on every edit


def test_badge_before_edit_no_reminder(tmp_path):
    script = [
        _tc("skill.load", json.dumps({"name": "j-space"})),
        _tc("run.badge", json.dumps({"label": "j-space: loop"})),
        _tc("fs.write", json.dumps({"path": "a.py", "content": "x"})),
        _final("done"),
    ]
    rt, seen = _rt(tmp_path, script)
    out = asyncio.run(rt.run("use the j-space skill for this"))
    assert out["status"] == "ok"
    assert _hints(seen) == set()


def test_skill_without_flag_gets_no_watch(tmp_path):
    no_flag = SKILL_MD.replace("requires_badge: true\n", "")
    script = [
        _tc("skill.load", json.dumps({"name": "j-space"})),
        _tc("fs.write", json.dumps({"path": "a.py", "content": "x"})),
        _final("done"),
    ]
    rt, seen = _rt(tmp_path, script, skill_md=no_flag)
    out = asyncio.run(rt.run("use the j-space skill for this"))
    assert out["status"] == "ok"
    assert _hints(seen) == set()
