"""Goal/progress anchor, the note.set scratchpad, and the no-progress breaker's
failure-signature signal."""
import asyncio
from runtime.loop import AgentRuntime, _verify_sig
from runtime.tool_base import ToolContext
from tools.agent.note import NoteSet


# ---- #1: verifier failure signature (the no-progress signal) ----
def test_sig_same_failure_ignores_noise():
    a = "FAILED test_x.py::test_a - AssertionError  in 0.03s  (/tmp/abc123/w)"
    b = "FAILED test_x.py::test_a - AssertionError  in 1.27s  (/tmp/zzz999/w)"
    assert _verify_sig(a) == _verify_sig(b)      # only durations/tmp paths differ


def test_sig_distinguishes_real_change():
    a = "FAILED test_x.py::test_a - AssertionError"
    b = "FAILED test_x.py::test_b - TypeError: nope"
    assert _verify_sig(a) != _verify_sig(b)


def test_stall_counter_trips_on_repeat_and_resets_on_progress():
    def run(reports, stall_after=2):
        st = {"sig": None, "count": 0}
        for i, rep in enumerate(reports, 1):
            sig = _verify_sig(rep)
            if sig == st["sig"]:
                st["count"] += 1
            else:
                st["sig"], st["count"] = sig, 1
            if st["count"] >= stall_after:
                return i
        return None
    assert run(["fail A (0.1s)", "fail A (0.2s)", "fail A (0.3s)"]) == 2   # same -> trips at 2nd
    assert run(["fail A", "fail B", "fail A"]) is None                     # alternating -> never


# ---- #2: goal + progress anchor ----
def test_anchor_none_without_goal():
    assert AgentRuntime._build_anchor("", "") is None
    assert AgentRuntime._build_anchor(None, "note") is None


def test_anchor_restates_goal_and_note():
    m = AgentRuntime._build_anchor("Fix the parser", "")
    assert m["role"] == "system" and "GOAL: Fix the parser" in m["content"]
    assert "PROGRESS NOTES" not in m["content"]
    m2 = AgentRuntime._build_anchor("Do X", "done: step 1\ntodo: step 2")
    assert "PROGRESS NOTES" in m2["content"] and "step 2" in m2["content"]


def test_anchor_truncates_long_goal():
    m = AgentRuntime._build_anchor("g" * 1000, "")
    assert "…" in m["content"] and len(m["content"]) < 950


# ---- #2: note.set tool ----
def test_note_set_writes_via_seam_and_strips():
    store = {}
    ctx = ToolContext(request_id="r", config={}, budget=None)
    ctx.set_note = lambda t: store.__setitem__("note", t)
    res = asyncio.run(NoteSet().execute({"text": "  my note  "}, ctx))
    assert res.status == "ok" and store["note"] == "my note"


def test_note_set_errors_without_seam():
    ctx = ToolContext(request_id="r", config={}, budget=None)   # set_note stays None
    res = asyncio.run(NoteSet().execute({"text": "x"}, ctx))
    assert res.status == "error"
