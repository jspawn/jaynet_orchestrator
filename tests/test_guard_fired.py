"""guard_fired telemetry (audit P2 step 4): every guard application in all
three registries emits one uniform guard_fired event — {"name", "phase",
"turn"} — IN ADDITION to the guard's legacy event/hint (those stay
byte-identical). One guard per phase, driven with the real loop over a
fake model (same harness as test_loop_regressions)."""
import asyncio
import json

from runtime.tool_base import ToolResult
from tests.test_loop_regressions import _final, _Registry, _runtime, _tc


def _events():
    seen = []

    async def cap(ev):
        seen.append(ev)
    return seen, cap


def _fired(events, name):
    return [e for e in events
            if e["type"] == "guard_fired" and e["data"]["name"] == name]


def test_pre_turn_guard_fired_alongside_legacy(tmp_path):
    """Stall ladder (pre_turn): two no-progress turns arm rung 1 — the
    legacy stall_check event AND a guard_fired event, same iteration,
    guard_fired after the legacy event."""
    from tools.fs.ops import FsRead
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    reg = _Registry([], real={"fs.read": FsRead()})
    script = [_tc("fs.read", json.dumps({"path": "a.txt"})),
              _tc("fs.read", json.dumps({"path": "b.txt"})),
              _final("done")]
    rt, _ = _runtime(reg, script)
    events, cap = _events()
    out = asyncio.run(rt.run("inspect", work_root=str(tmp_path),
                             on_event=cap))
    assert out["status"] == "ok"
    fired = _fired(events, "stall_check")
    assert len(fired) == 1
    assert fired[0]["data"] == {"name": "stall_check", "phase": "pre_turn",
                                "turn": fired[0]["iteration"]}
    legacy = [e for e in events if e["type"] == "stall_check"]
    assert len(legacy) == 1
    assert legacy[0]["iteration"] == fired[0]["data"]["turn"]
    assert events.index(legacy[0]) < events.index(fired[0])


class _CrashRun:
    """code.run stand-in that always fails the same way (same payload
    signature → the failure-streak guard's consecutive counter)."""
    private = False
    name = "code.run"

    def needs_confirmation(self, args, ctx): return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        return ToolResult(status="ok",
                          result={"ok": False, "exit_code": 1, "stderr": "boom"})


def test_post_tool_guard_fired_with_hint(tmp_path):
    """Failure streak (post_tool): three consecutive same-signature
    failures fire the strategy-change hint (no legacy event — the hint IS
    the legacy signal) plus one guard_fired per firing."""
    reg = _Registry([], real={"code.run": _CrashRun()})
    script = [_tc("code.run", json.dumps({"command": "try 1"})),
              _tc("code.run", json.dumps({"command": "try 2"})),
              _tc("code.run", json.dumps({"command": "try 3"})),
              _final("done")]
    rt, seen = _runtime(reg, script)
    events, cap = _events()
    out = asyncio.run(rt.run("run it", work_root=str(tmp_path),
                             on_event=cap))
    assert out["status"] == "ok"
    fired = _fired(events, "failure_streak")
    assert len(fired) == 1
    assert fired[0]["data"] == {"name": "failure_streak",
                                "phase": "post_tool",
                                "turn": fired[0]["iteration"]}
    # The legacy signal: the strategy-change hint rode the third tool
    # result into the transcript.
    assert any("same error signature" in str(m.get("content"))
               for msgs in seen for m in msgs if m.get("role") == "tool")


def test_final_answer_guard_fired_alongside_legacy():
    """Empty final answer (final_answer): the empty-final bounce emits its
    legacy empty_final event AND guard_fired, same iteration, guard_fired
    after the legacy event."""
    rt, _ = _runtime(_Registry([]), [_final(""), _final("done")])
    events, cap = _events()
    out = asyncio.run(rt.run("hi", on_event=cap))
    assert out["status"] == "ok" and out["answer"] == "done"
    fired = _fired(events, "empty")
    assert len(fired) == 1
    assert fired[0]["data"] == {"name": "empty", "phase": "final_answer",
                                "turn": fired[0]["iteration"]}
    legacy = [e for e in events if e["type"] == "empty_final"]
    assert len(legacy) == 1
    assert legacy[0]["iteration"] == fired[0]["data"]["turn"]
    assert events.index(legacy[0]) < events.index(fired[0])
