"""Stall ladder (agent.stall_check): consecutive no-progress turns (only
reads/searches, no mutation) earn escalating directives — act now → dumbest
version/delegate/ask → produce-or-ask. Progress resets the counter, rungs
already fired stay fired, poll-only turns are neutral. Driven with the real
loop over a fake model — no network, no LiteLLM."""
import asyncio
import json

from runtime.tool_base import ToolResult
from tests.test_loop_regressions import _final, _Registry, _runtime, _tc


def _read(path):
    return _tc("fs.read", json.dumps({"path": path}))


def _reg(tmp_path, *names):
    from tools.fs.ops import FsRead
    for n in names:
        (tmp_path / n).write_text(n)
    return _Registry([], real={"fs.read": FsRead()})


def _stall_msgs(seen):
    """Distinct stall-ladder injections seen across all turns (an injected
    system message persists in the transcript, so dedupe by content)."""
    out = set()
    for msgs in seen:
        for m in msgs:
            if m.get("role") == "system" and isinstance(m.get("content"), str):
                c = m["content"]
                if c.startswith(("Progress check", "You still have not",
                                 "Final progress warning")):
                    out.add(c)
    return out


def test_ladder_fires_all_rungs_once(tmp_path):
    reg = _reg(tmp_path, "a.txt", "b.txt", "c.txt", "d.txt", "e.txt", "f.txt")
    script = [_read(p) for p in ("a.txt", "b.txt", "c.txt",
                                 "d.txt", "e.txt", "f.txt")] + [_final("done")]
    rt, seen = _runtime(reg, script)
    rt.config["budgets"] = {**rt.config["budgets"], "max_iterations": 40}
    out = asyncio.run(rt.run("inspect a lot", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    msgs = _stall_msgs(seen)
    assert sum(m.startswith("Progress check") for m in msgs) == 1
    assert sum(m.startswith("You still have not") for m in msgs) == 1
    assert sum(m.startswith("Final progress warning") for m in msgs) == 1


def test_progress_resets_the_counter(tmp_path):
    """read,read → rung 1; a mutation resets, so rung 2 needs `after`*(rung)
    fresh no-progress turns (4 with the default after=2) — not just 2 more."""
    from tests.test_loop_regressions import _TouchFile
    from tools.fs.ops import FsRead
    (tmp_path / "a.txt").write_text("a")
    reg = _Registry([], real={"fs.read": FsRead(), "x.touch": _TouchFile()})
    script = [
        _read("a.txt"), _read("a.txt"),          # stall 1,2 → rung 1 next turn
        _tc("x.touch", "{}"),                    # progress → reset
        _read("a.txt"), _read("a.txt"), _read("a.txt"),  # stall 1..3, no rung
        _read("a.txt"),                          # stall 4 → rung 2 next turn
        _final("done"),
    ]
    rt, seen = _runtime(reg, script)
    rt.config["budgets"] = {**rt.config["budgets"], "max_iterations": 40}
    # distinct-enough args to dodge the duplicate guard: same path repeated
    # only twice per generation (the touch re-generates).
    out = asyncio.run(rt.run("read, touch, read", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    msgs = _stall_msgs(seen)
    assert sum(m.startswith("Progress check") for m in msgs) == 1
    assert sum(m.startswith("You still have not") for m in msgs) == 1
    assert sum(m.startswith("Final progress warning") for m in msgs) == 0


def test_disabled_injects_nothing(tmp_path):
    reg = _reg(tmp_path, "a.txt", "b.txt", "c.txt")
    script = [_read("a.txt"), _read("b.txt"), _read("c.txt"), _final("done")]
    rt, seen = _runtime(reg, script)
    rt.config["agent"] = {"stall_check": {"enabled": False}}
    out = asyncio.run(rt.run("inspect", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert _stall_msgs(seen) == set()


class _PollStub:
    """Poll-safe probe stand-in (job.status shape): succeeds, mutates nothing,
    and is registered in rt._poll_safe so the ladder treats it as neutral."""
    private = False
    name = "job.status"

    def needs_confirmation(self, args, ctx): return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        return ToolResult(status="ok", result={"state": "running"})


def test_poll_only_turns_are_neutral(tmp_path):
    """Polling a running job is waiting, not stalling: it must neither count
    toward the ladder nor reset an already-running no-progress streak."""
    from tools.fs.ops import FsRead
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    reg = _Registry([], real={"fs.read": FsRead(), "job.status": _PollStub()})
    script = [
        _read("a.txt"),                                   # stall 1
        _tc("job.status", "{}"), _tc("job.status", "{}"), # neutral
        _read("b.txt"),                                   # stall 2 → rung 1
        _final("done"),
    ]
    rt, seen = _runtime(reg, script)
    rt._poll_safe = {"job.status"}
    out = asyncio.run(rt.run("read, poll, read", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    msgs = _stall_msgs(seen)
    assert sum(m.startswith("Progress check") for m in msgs) == 1


class _BookkeepStub:
    """Bookkeeping stand-in (todos shape): no read_only marker, so a success
    bumps the mutation generation — yet a turn of ONLY bookkeeping must not
    reset the stall ladder (live: tb-huarong hid in 11 todos turns)."""
    private = False
    name = "todos"

    def needs_confirmation(self, args, ctx): return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        return ToolResult(status="ok", result={"items": []})


class _CheckStub(_BookkeepStub):
    """Verify-only exec stand-in (code.check shape): succeeds, writes nothing,
    bumps the mutation generation — still must not reset the ladder (live:
    tb-huarong re-ran one analysis script 14 times after the todos fix)."""
    name = "code.check"

    async def execute(self, args, ctx):
        return ToolResult(status="ok", result={"exit": 0, "stdout": "ok"})


def test_verify_only_exec_streaks_do_not_reset_the_ladder(tmp_path):
    """An artifact write resets; a following streak of code.check-only turns
    (no intervening edit) is exec-spinning — rungs keep escalating."""
    from tests.test_loop_regressions import _TouchFile
    reg = _Registry([], real={"x.touch": _TouchFile(), "code.check": _CheckStub()})
    script = [
        _tc("x.touch", "{}"),                                # artifact → reset
        _tc("code.check", "{}"), _tc("code.check", "{}"),    # stall 1,2 → rung 1
        _tc("code.check", "{}"), _tc("code.check", "{}"),    # stall 3,4 → rung 2
        _tc("code.check", "{}"), _tc("code.check", "{}"),    # stall 5,6 → rung 3
        _final("done"),
    ]
    rt, seen = _runtime(reg, script)
    rt.config["budgets"] = {**rt.config["budgets"], "max_iterations": 40}
    out = asyncio.run(rt.run("check forever", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    msgs = _stall_msgs(seen)
    assert sum(m.startswith("Progress check") for m in msgs) == 1
    assert sum(m.startswith("You still have not") for m in msgs) == 1
    assert sum(m.startswith("Final progress warning") for m in msgs) == 1


def test_edit_between_checks_still_resets(tmp_path):
    """Legit verify loops interleave edits: a mutation between code.check
    turns resets the counter, so rung 2 needs `after`*(rung) FRESH streaks."""
    from tests.test_loop_regressions import _TouchFile
    reg = _Registry([], real={"x.touch": _TouchFile(), "code.check": _CheckStub()})
    script = [
        _tc("code.check", "{}"), _tc("code.check", "{}"),  # stall 1,2 → rung 1
        _tc("x.touch", "{}"),                              # edit → reset
        _tc("code.check", "{}"), _tc("code.check", "{}"),
        _tc("code.check", "{}"),                           # stall 1..3, no rung
        _tc("code.check", "{}"),                           # stall 4 → rung 2
        _final("done"),
    ]
    rt, seen = _runtime(reg, script)
    rt.config["budgets"] = {**rt.config["budgets"], "max_iterations": 40}
    out = asyncio.run(rt.run("check, fix, check", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    msgs = _stall_msgs(seen)
    assert sum(m.startswith("Progress check") for m in msgs) == 1
    assert sum(m.startswith("You still have not") for m in msgs) == 1
    assert sum(m.startswith("Final progress warning") for m in msgs) == 0


def test_bookkeeping_only_turns_do_not_reset_the_ladder(tmp_path):
    """reads → rung 1; endless todos updates in between must NOT hold the
    ladder at rung 1 — the rungs keep escalating across bookkeeping turns."""
    from tools.fs.ops import FsRead
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    reg = _Registry([], real={"fs.read": FsRead(), "todos": _BookkeepStub()})
    script = [
        _read("a.txt"), _read("b.txt"),          # stall 1,2 → rung 1 next turn
        _tc("todos", "{}"), _tc("todos", "{}"),  # bookkeeping, no reset
        _tc("todos", "{}"), _tc("todos", "{}"),  # stall 3,4 → rung 2 next turn
        _tc("todos", "{}"), _tc("todos", "{}"),  # stall 5,6 → rung 3 next turn
        _final("done"),
    ]
    rt, seen = _runtime(reg, script)
    rt.config["budgets"] = {**rt.config["budgets"], "max_iterations": 40}
    out = asyncio.run(rt.run("plan forever", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    msgs = _stall_msgs(seen)
    assert sum(m.startswith("Progress check") for m in msgs) == 1
    assert sum(m.startswith("You still have not") for m in msgs) == 1
    assert sum(m.startswith("Final progress warning") for m in msgs) == 1


def test_identical_success_repeats_do_not_reset_the_ladder(tmp_path):
    """A byte-identical repeat of an earlier OK mutation is a no-op — the
    live Taichu rewrite loop (45× the same fs.write) held the ladder at
    rung 1 because every write bumped the mutation generation. Identical
    twins must not reset the counter; a DISTINCT mutation still does."""
    from tests.test_loop_regressions import _TouchFile
    from tools.fs.ops import FsRead
    (tmp_path / "a.txt").write_text("a")
    reg = _Registry([], real={"fs.read": FsRead(), "x.touch": _TouchFile()})
    script = [
        _read("a.txt"), _read("a.txt"),          # stall 1,2 → rung 1 next turn
        _tc("x.touch", json.dumps({"p": 1})),    # progress → reset
        _tc("x.touch", json.dumps({"p": 1})),    # identical twin → NO reset
        _read("a.txt"), _read("a.txt"),          # stall 2,3
        _read("a.txt"), _read("a.txt"),          # stall 4,5 → rung 2 fires
        _final("done"),
    ]
    rt, seen = _runtime(reg, script)
    rt.config["budgets"] = {**rt.config["budgets"], "max_iterations": 40}
    out = asyncio.run(rt.run("rewrite loop", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    msgs = _stall_msgs(seen)
    assert sum(m.startswith("Progress check") for m in msgs) == 1
    assert sum(m.startswith("You still have not") for m in msgs) == 1


def test_distinct_mutation_after_repeat_still_resets(tmp_path):
    """The twin-tracking must not overreach: touch A, touch A (no reset),
    then touch B — a genuinely new mutation — resets the ladder."""
    from tests.test_loop_regressions import _TouchFile
    from tools.fs.ops import FsRead
    (tmp_path / "a.txt").write_text("a")
    reg = _Registry([], real={"fs.read": FsRead(), "x.touch": _TouchFile()})
    script = [
        _read("a.txt"), _read("a.txt"),          # stall 1,2 → rung 1 next turn
        _tc("x.touch", json.dumps({"p": 1})),    # progress → reset
        _tc("x.touch", json.dumps({"p": 1})),    # twin → stall 1
        _tc("x.touch", json.dumps({"p": 2})),    # distinct → reset
        _read("a.txt"), _read("a.txt"), _read("a.txt"),  # stall 1..3, no rung
        _final("done"),
    ]
    rt, seen = _runtime(reg, script)
    rt.config["budgets"] = {**rt.config["budgets"], "max_iterations": 40}
    out = asyncio.run(rt.run("repeat then real edit", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    msgs = _stall_msgs(seen)
    assert sum(m.startswith("Progress check") for m in msgs) == 1
    assert sum(m.startswith("You still have not") for m in msgs) == 0
