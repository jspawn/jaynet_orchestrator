"""Guard ablation (code audit 2026-09-23, "Guard ablation"): eval benchmark
variants disable named guards via `guards_off` — threaded from the variant
spec through run_case into runtime.run, which drops them from all three
registries at the per-run instantiation point (loop.py). Unknown guard
names fail the variant config loudly at case load, never mid-run."""
import asyncio

import pytest
from conftest import run

from runtime import eval_runner
from runtime.eval_store import EvalStore
from tests.test_eval_harness import _case, _FakeRuntime, _judge_ok
from tests.test_loop_regressions import _final, _Registry, _runtime


def _events():
    seen = []

    async def cap(ev):
        seen.append(ev)
    return seen, cap


def test_known_guard_names_cover_all_registries():
    """The legal guards_off values are the three registries' guard names."""
    names = eval_runner.known_guard_names()
    for n in ("just_reply", "stall_check", "verify_delegate", "deliverable",
              "failure_streak", "host_give_up", "badge_watch", "wrap_up",
              "budget_warning"):
        assert n in names
    assert eval_runner.check_guards_off(["just_reply"]) == []
    assert eval_runner.check_guards_off(["just_rply"]) == ["just_rply"]


def test_guards_off_prevents_guard_event():
    """fake-model harness: with just_reply ablated, a tool-less compute
    answer is accepted immediately — no just_reply_check legacy event and
    no guard_fired for it. Control: all guards on, the same script bounces
    once (the rail fires)."""
    rt, _ = _runtime(_Registry([]), [_final("12000")])
    events, cap = _events()
    out = asyncio.run(rt.run("How many widgets? Count exactly.",
                             guards_off=["just_reply"], on_event=cap))
    assert out["status"] == "ok" and out["answer"] == "12000"
    assert not [e for e in events if e["type"] == "just_reply_check"]
    assert not [e for e in events
                if e["type"] == "guard_fired"
                and e["data"]["name"] == "just_reply"]

    rt2, _ = _runtime(_Registry([]), [_final("12000"), _final("16000")])
    events2, cap2 = _events()
    out2 = asyncio.run(rt2.run("How many widgets? Count exactly.",
                               on_event=cap2))
    assert out2["answer"] == "16000"
    assert len([e for e in events2 if e["type"] == "just_reply_check"]) == 1


def test_guards_off_pre_turn_guard(tmp_path):
    """stall_check ablated: a no-progress run never sees the stall-ladder
    rung (the pre-turn registry is filtered too). Same harness as
    test_guard_fired's pre-turn case."""
    import json

    from tests.test_loop_regressions import _tc
    from tools.fs.ops import FsRead
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    script = [_tc("fs.read", json.dumps({"path": "a.txt"})),
              _tc("fs.read", json.dumps({"path": "b.txt"})),
              _final("done")]
    rt, _ = _runtime(_Registry([], real={"fs.read": FsRead()}), script)
    events, cap = _events()
    out = asyncio.run(rt.run("inspect", work_root=str(tmp_path),
                             guards_off=["stall_check"], on_event=cap))
    assert out["status"] == "ok"
    assert not [e for e in events if e["type"] == "stall_check"]
    # control: same script with all rails on fires rung 1
    rt2, _ = _runtime(_Registry([], real={"fs.read": FsRead()}), script)
    events2, cap2 = _events()
    asyncio.run(rt2.run("inspect", work_root=str(tmp_path), on_event=cap2))
    assert len([e for e in events2 if e["type"] == "stall_check"]) == 1


def test_eval_variant_threads_guards_off(tmp_path, monkeypatch):
    """run_case hands the variant's guards_off to runtime.run."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["ok"])
    store = EvalStore(tmp_path / "eval.db")
    run(eval_runner.run_case(rt, _case(), store,
                             variant={"label": "v",
                                      "guards_off": ["just_reply"]}))
    assert rt.calls[0][1]["guards_off"] == ["just_reply"]
    store.close()
    # default: no ablation → None (all guards)
    rt2 = _FakeRuntime(["ok"])
    store2 = EvalStore(tmp_path / "eval2.db")
    run(eval_runner.run_case(rt2, _case(), store2,
                             variant={"label": "v"}))
    assert rt2.calls[0][1]["guards_off"] is None
    store2.close()


def test_eval_unknown_guard_fails_at_case_load(tmp_path, monkeypatch):
    """A typo'd guard name raises ValueError before the first model call —
    loudly, at case load, so the ablation can never silently run with all
    rails on."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["ok"])
    store = EvalStore(tmp_path / "eval.db")
    with pytest.raises(ValueError, match="unknown guard"):
        run(eval_runner.run_case(rt, _case(), store,
                                 variant={"label": "v",
                                          "guards_off": ["just_rply"]}))
    assert not rt.calls
    store.close()


def test_run_suite_validates_variant_up_front(tmp_path, monkeypatch):
    """The suite fails before spending on case 1 (run_suite's per-case
    crash handler must not turn a bad variant spec into a crash row)."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["ok"])
    store = EvalStore(tmp_path / "eval.db")
    with pytest.raises(ValueError, match="unknown guard"):
        run(eval_runner.run_suite(rt, [_case()], store,
                                  variant={"label": "v",
                                           "guards_off": ["nope"]}))
    assert not rt.calls
    store.close()


def test_deliverable_ablation_still_pins_the_answer():
    """Audit #23 B1: the final_answer pin rode the deliverable guard's
    membership in the ablation-filtered registry, so guards_off=
    ["deliverable"] finished with answer == "" and the ablation column
    graded an artifact. Ablated guards now skip only their CHECK — the
    registry (and the pin's legacy position) stays complete."""
    rt, _ = _runtime(_Registry([]), [_final("the pinned answer")])
    out = asyncio.run(rt.run("answer me", guards_off=["deliverable"]))
    assert out["status"] == "ok" and out["answer"] == "the pinned answer"
