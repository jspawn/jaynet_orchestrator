"""runtime/reflect.py — in-chat correction capture: the lexical pre-gate,
the local-model verdict (JSON extraction, skill-target validation), and the
full maybe_capture path into the dedup'd proposals inbox. No network: the
analysis call is monkeypatched."""
from __future__ import annotations

import pytest
from conftest import run

from runtime import reflect
from runtime.eval_store import EvalStore
from runtime.tool_base import ToolResult

CFG_ON = {"reflect": {"enabled": True, "max_message_chars": 800}}
CFG_OFF = {"reflect": {"enabled": False}}


# ---- lexical pre-gate --------------------------------------------------------

@pytest.mark.parametrize("msg", [
    "No, use uv instead of pip!",
    "never delete the lockfile",
    "Always check the tests before finishing.",
    "stop using unittest",
    "Don't forget to run ruff",
    "do not send private data to the cloud",
    "from now on, prefer pytest",
    "use ruff instead of pylint, please",
    "ab jetzt immer uv verwenden",
    "nie wieder pip benutzen",
])
def test_gate_accepts_corrections(msg):
    assert reflect.looks_like_correction(CFG_ON, msg) is True


@pytest.mark.parametrize("msg", [
    "What is the deadline for the report?",
    "yes, that works, thanks",
    "the build is green now",
    "/imp kimi-k3",
    "",
    "please explain the difference between uv and pip",
])
def test_gate_rejects_non_corrections(msg):
    assert reflect.looks_like_correction(CFG_ON, msg) is False


def test_gate_disabled_and_long_messages():
    assert reflect.looks_like_correction(
        CFG_OFF, "never use pip") is False
    assert reflect.looks_like_correction(
        CFG_ON, "never use pip " + "x" * 900) is False


# ---- model verdict -----------------------------------------------------------

class _RT:
    def __init__(self):
        self.config = dict(CFG_ON)


def _ok(text):
    return ToolResult(status="ok", result=text, tool_name="llm.call")


def _patch_call(monkeypatch, reply):
    from tools.llm import cloud_models

    async def fake(alias, task, payload, system, want_json, think, ctx):
        assert alias == "local-orchestrator"   # pinned local, never cloud
        return _ok(reply)
    monkeypatch.setattr(cloud_models, "_call_via_litellm", fake)


GOOD = ('{"teaching": true, "what": "User wants uv not pip",'
        ' "cause": "skill instructions still say pip",'
        ' "fix": "Always use uv instead of pip.",'
        ' "classification": "skill-tweak", "target": "coding"}')


def test_analyze_parses_teaching(monkeypatch):
    _patch_call(monkeypatch, GOOD)
    d = run(reflect.analyze(_RT(), message="no, use uv instead of pip",
                            answer="pip install fastapi", skills=["coding"]))
    assert d["classification"] == "skill-tweak"
    assert d["target"] == "coding"
    assert d["fix"] == "Always use uv instead of pip."


def test_analyze_rejects_non_teaching_and_drift(monkeypatch):
    _patch_call(monkeypatch, '{"teaching": false}')
    assert run(reflect.analyze(_RT(), message="the deadline is friday",
                               answer="ok", skills=[])) is None
    _patch_call(monkeypatch, "not json at all")
    assert run(reflect.analyze(_RT(), message="never use pip",
                               answer="ok", skills=[])) is None
    _patch_call(monkeypatch, '{"teaching": true, "what": "", "fix": ""}')
    assert run(reflect.analyze(_RT(), message="never use pip",
                               answer="ok", skills=[])) is None


def test_analyze_downgrades_hallucinated_skill(monkeypatch):
    """A skill-tweak targeting a skill that was NOT loaded is a hallucination
    — downgrade to prompt-tweak instead of proposing into the void."""
    _patch_call(monkeypatch, GOOD)
    d = run(reflect.analyze(_RT(), message="no, use uv instead of pip",
                            answer="pip install x", skills=["deep-research"]))
    assert d["classification"] == "prompt-tweak"
    assert d["target"] is None


# ---- full capture path --------------------------------------------------------

def _capture(monkeypatch, tmp_path, reply, *, skills=(), owner="jay",
             message="no, use uv instead of pip"):
    _patch_call(monkeypatch, reply)
    monkeypatch.setattr(reflect.paths, "EVAL_DB", tmp_path / "eval.db")
    from runtime import eval_runner
    monkeypatch.setattr(eval_runner, "_skill_loads_from_trace",
                        lambda ids: set(skills))
    rt = _RT()
    return run(reflect.maybe_capture(rt, message=message, answer="pip x",
                                     run_ids=["r1"], owner=owner)), rt


def test_capture_creates_deduped_proposal(monkeypatch, tmp_path):
    prop, _ = _capture(monkeypatch, tmp_path, GOOD, skills=["coding"])
    assert prop is not None
    assert prop["test_id"] == "reflect:jay"
    assert prop["classification"] == "skill-tweak"
    assert prop["status"] == "pending"
    # same teaching again → dedup refresh, not a second inbox row
    prop2, _ = _capture(monkeypatch, tmp_path, GOOD, skills=["coding"])
    store = EvalStore(tmp_path / "eval.db")
    try:
        rows = store.proposals()
    finally:
        store.close()
    assert len(rows) == 1
    assert prop2["id"] == prop["id"]


def test_capture_gate_short_circuits(monkeypatch, tmp_path):
    """A non-correction never reaches the model or the store."""
    prop, _ = _capture(monkeypatch, tmp_path, GOOD,
                       message="what time is it?")
    assert prop is None
    store = EvalStore(tmp_path / "eval.db")
    try:
        assert store.proposals() == []
    finally:
        store.close()


def test_capture_non_teaching_verdict_writes_nothing(monkeypatch, tmp_path):
    prop, _ = _capture(monkeypatch, tmp_path, '{"teaching": false}')
    assert prop is None
    store = EvalStore(tmp_path / "eval.db")
    try:
        assert store.proposals() == []
    finally:
        store.close()
