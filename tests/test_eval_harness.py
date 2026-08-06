"""Eval harness core: case loading, store, expectations, runner (fake runtime).

No network, no real models: the runner is driven against a stub AgentRuntime
and a stubbed _model_text (judge/driver).
"""
from __future__ import annotations

import pytest
from pathlib import Path

from runtime import eval_runner
from runtime.eval_cases import (EvalCase, load_cases, parse_case,
                                validate_case_dict)
from runtime.eval_store import EvalStore

from conftest import run


# ---- case loading -----------------------------------------------------------

_VALID = """
name: Test case
tags: [a, b]
driver: scripted
turns:
  - user: "hello"
expect:
  must_use_tools: [fs.read]
judge_rubric: "Pass if polite."
"""


def test_parse_valid_case():
    c = parse_case("demo", _VALID, "builtin")
    assert c.id == "demo" and c.name == "Test case"
    assert c.driver == "scripted" and c.turns == ["hello"]
    assert c.expect["must_use_tools"] == ["fs.read"]


@pytest.mark.parametrize("raw,needle", [
    ("{}", "name is required"),
    ("name: x\nturns: []", "at least one turn"),
    ("name: x\nturns: [{user: hi}]\njudge_rubric: r\ndriver: weird", "driver"),
    ("name: x\nturns: [{user: hi}]\njudge_rubric: r\nexpect: {bogus: 1}",
     "unknown expect key"),
    ("name: x\nturns: [{user: hi}]\njudge_rubric: ''", "judge_rubric"),
    ("name: x\nturns: [{user: hi}]\njudge_rubric: r\nexpect: {max_iterations: -1}",
     "positive integer"),
])
def test_validate_errors(raw, needle):
    import yaml
    errors = validate_case_dict("demo", yaml.safe_load(raw))
    assert any(needle in e for e in errors), errors


def test_custom_layer_overrides_builtin(tmp_path):
    builtin = tmp_path / "builtin"
    custom = tmp_path / "custom"
    builtin.mkdir()
    custom.mkdir()
    (builtin / "demo.yaml").write_text(_VALID.replace("Test case", "Builtin"))
    (custom / "demo.yaml").write_text(_VALID.replace("Test case", "Custom"))
    (builtin / "only-builtin.yaml").write_text(_VALID)
    cases = {c.id: c for c in load_cases(builtin, custom)}
    assert cases["demo"].name == "Custom"
    assert cases["demo"].origin == "custom"
    assert cases["only-builtin"].origin == "builtin"


_REPO = Path(__file__).resolve().parent.parent   # the orch-dev checkout


def test_shipped_seeds_all_valid():
    cases = load_cases(builtin_dir=_REPO / "evals", custom_dir="/nonexistent")
    ids = {c.id for c in cases}
    assert {"web-freshness", "fs-roundtrip", "sycophancy-probe",
            "datetime-awareness"} <= ids


# ---- store -------------------------------------------------------------------

def test_store_results_trend_and_latest(tmp_path):
    s = EvalStore(tmp_path / "eval.db")
    for i, passed in enumerate([True, False, True]):
        s.record_result(test_id="t1", passed=passed, score=5 + i,
                        judge_notes=f"n{i}", judge_model="m", cost_usd=0.01,
                        tokens=100, elapsed_s=1.0, status="ok",
                        run_ids=[f"r{i}"], transcript=[{"user": "x"}])
    latest = s.latest_by_test()["t1"]
    assert latest["passed"] == 1 and latest["score"] == 7
    trend = s.trend("t1")
    assert [t["passed"] for t in trend] == [1, 0, 1]
    assert len(s.results("t1")) == 3
    s.close()


def test_store_proposals_dedup_and_status(tmp_path):
    s = EvalStore(tmp_path / "eval.db")
    p1 = s.add_proposal(test_id="t", result_id=1, classification="config",
                        what="w", cause="c", fix="f")
    assert p1 and p1["status"] == "pending"
    # same classification+cause+fix → dedup, even for another test
    assert s.add_proposal(test_id="t2", result_id=2, classification="config",
                          what="w2", cause="C", fix="F") is None
    s.set_proposal_status(p1["id"], "accepted")
    assert s.proposals("accepted")[0]["id"] == p1["id"]
    assert s.proposals("pending") == []
    with pytest.raises(ValueError):
        s.set_proposal_status(p1["id"], "bogus")
    s.close()


# ---- expectations --------------------------------------------------------------

def _turn(traj="", answer="", iterations=1, tools=None):
    t = {"trajectory": traj, "answer": answer,
         "budget": {"iterations": iterations}}
    if tools is not None:
        t["tools"] = tools
    return t


def test_check_expectations():
    case = EvalCase(id="x", name="x", turns=["hi"],
                    expect={"must_use_tools": ["web.search"],
                            "must_not_use_tools": ["llm.call"],
                            "answer_contains_any": ["2026"],
                            "max_iterations": 3},
                    judge_rubric="r")
    good = [_turn("web.search(q)→ok", "costs 42 in 2026", 2)]
    assert eval_runner.check_expectations(case, good) == []
    bad = [_turn("llm.call(q)→ok; llm.call(q)→ok", "dunno", 5)]
    failures = eval_runner.check_expectations(case, bad)
    assert len(failures) == 4     # missing web.search, forbidden llm.call,
    assert any("web.search" in f for f in failures)      # no 2026, over cap
    assert any("llm.call" in f for f in failures)


class _FakeToolResult:
    def __init__(self, status="ok", error=None):
        self.status = status
        self.error = error


def test_check_expectations_hintless_tools_structural():
    """Audit B1 regression: tools whose args carry no trajectory hint
    (memory.append/ask.user/code.run) appear BARE in the display trajectory,
    so the regex fallback can never see them — the structural tools_used list
    must. Uses the loop's real _traj_entry, not a hand-written string."""
    from runtime.loop import _traj_entry
    traj = _traj_entry("memory.append", {"content": "x"},
                       _FakeToolResult())
    assert traj == "memory.append→ok"          # no (hint) — the B1 trap
    case = EvalCase(id="m", name="m", turns=["hi"],
                    expect={"must_use_tools": ["memory.append"]},
                    judge_rubric="r")
    # regex-only view (legacy rows) misses it...
    assert eval_runner.check_expectations(case, [_turn(traj)]) != []
    # ...the structural list catches it
    assert eval_runner.check_expectations(
        case, [_turn(traj, tools=["memory.append"])]) == []


def test_check_expectations_year_placeholders():
    import time as _t
    year = str(_t.localtime().tm_year)
    case = EvalCase(id="d", name="d", turns=["hi"],
                    expect={"answer_contains_any": ["{year}", "{next_year}"]},
                    judge_rubric="r")
    assert eval_runner.check_expectations(case, [_turn(answer=year)]) == []
    assert eval_runner.check_expectations(case, [_turn(answer="1999")]) != []


def test_patch_tools_config_never_mutates_shared():
    from runtime.loop import _patch_tools_config
    base = {"tools": {"memory": {"db_path": "/real/memory.db"}}, "other": 1}
    patched = _patch_tools_config(base, {"memory": {"db_path": "/tmp/m.db"},
                                         "rag": {"db_path": "/tmp/r.db"}})
    assert patched["tools"]["memory"]["db_path"] == "/tmp/m.db"
    assert patched["tools"]["rag"]["db_path"] == "/tmp/r.db"
    assert base["tools"]["memory"]["db_path"] == "/real/memory.db"
    assert "rag" not in base["tools"]
    assert _patch_tools_config(base, None) is base


def test_parse_json_tolerant():
    assert eval_runner._parse_json('{"pass": true}') == {"pass": True}
    assert eval_runner._parse_json('Sure! ```json\n{"pass": false}\n```') == {"pass": False}
    assert eval_runner._parse_json("no json here") is None


# ---- runner against a fake runtime ---------------------------------------------

class _FakeTool:
    def __init__(self, name, gated=False):
        self.name = name
        self.requires_confirmation = gated


class _FakeRegistry:
    def all(self):
        return [_FakeTool("fs.read"), _FakeTool("web.search"),
                _FakeTool("fs.delete", gated=True), _FakeTool("eval.run")]


class _FakeRuntime:
    """Stands in for AgentRuntime: canned per-message answers."""
    def __init__(self, answers):
        self.config = {"eval": {"judge_temperature": 0.0},
                       "privacy": {}, "costs": {}}
        self.registry = _FakeRegistry()
        self.model = "fake-brain"      # recorded as results.brain by run_case
        self._answers = list(answers)
        self.calls = []

    async def run(self, message, **kwargs):
        self.calls.append((message, kwargs))
        answer = self._answers.pop(0) if self._answers else "ok"
        return {"run_id": f"fake-{len(self.calls)}", "status": "ok",
                "answer": answer,
                "trajectory": "web.search(q)→ok",
                "tools_used": ["web.search"],
                "budget": {"iterations": 2, "cost_usd": 0.0,
                           "tokens": {"total": 10}}}


async def _judge_ok(cfg, alias, messages, **kw):
    return {"status": "ok", "model_name": "fake-judge", "cost_usd": 0.001,
            "tokens": 50, "error": None,
            "content": '{"pass": true, "score": 9, "notes": "fine",'
                       ' "classification": "none"}'}


def _case(**kw):
    base = dict(id="demo", name="Demo", turns=["hi"],
                expect={"must_use_tools": ["web.search"]},
                judge_rubric="rubric")
    base.update(kw)
    return EvalCase(**base)


def test_run_case_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["all prices are 2026"])
    store = EvalStore(tmp_path / "eval.db")
    row = run(eval_runner.run_case(rt, _case(), store))
    assert row["passed"] is True or row["passed"] == 1
    assert row["judge_model"] == "fake-judge"
    # unattended wiring: gated tools + eval.run excluded, confirmations denied
    kwargs = rt.calls[0][1]
    assert "fs.delete" not in kwargs["tools"]
    assert "eval.run" not in kwargs["tools"]
    assert kwargs["owner"] == "_eval"
    assert kwargs["budget_overrides"]["max_iterations"] == 0
    # persistent stores redirected into the per-case sandbox (audit S2)
    patch = kwargs["run_overrides"]["tools_patch"]
    assert patch["memory"]["db_path"].endswith("memory.db")
    assert "/srv/" not in patch["memory"]["db_path"]
    assert patch["rag"]["db_path"].endswith("rag.db")
    # recorded
    assert store.results("demo")
    store.close()


def test_run_case_disabled_hook(tmp_path, monkeypatch):
    """Audit B5: the agent-initiated path (no explicit disabled_tools) must
    honour the globally disabled list via the hook."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    eval_runner.set_disabled_hook(lambda: ["web.search"])
    try:
        rt = _FakeRuntime(["ok"])
        store = EvalStore(tmp_path / "eval.db")
        run(eval_runner.run_case(rt, _case(), store))
        assert "web.search" not in rt.calls[0][1]["tools"]
        store.close()
    finally:
        eval_runner.set_disabled_hook(None)


def test_run_case_failure_writes_proposal(tmp_path, monkeypatch):
    async def judge_fail(cfg, alias, messages, **kw):
        return {"status": "ok", "model_name": "j", "cost_usd": 0.0,
                "tokens": 1, "error": None,
                "content": '{"pass": false, "score": 2, "notes": "bad",'
                           ' "classification": "prompt-tweak", "what": "w",'
                           ' "cause": "c", "fix": "f"}'}
    monkeypatch.setattr(eval_runner, "_model_text", judge_fail)
    rt = _FakeRuntime(["nope"])   # no web.search in answer; trajectory has it
    store = EvalStore(tmp_path / "eval.db")
    case = _case(expect={"answer_contains_any": ["ZZZ-missing"]})
    row = run(eval_runner.run_case(rt, case, store))
    assert not row["passed"]
    assert row["check_failures"]
    props = store.proposals("pending")
    assert len(props) == 1 and props[0]["classification"] == "prompt-tweak"
    store.close()


def test_run_case_adaptive_driver(tmp_path, monkeypatch):
    probes = iter(['{"message": "and for kids?"}', '{"done": true}'])

    async def driver_then_judge(cfg, alias, messages, **kw):
        system = messages[0]["content"]
        if "play the USER" in system:
            return {"status": "ok", "model_name": "d", "cost_usd": 0.0,
                    "tokens": 1, "error": None, "content": next(probes)}
        return {"status": "ok", "model_name": "j", "cost_usd": 0.0,
                "tokens": 1, "error": None,
                "content": '{"pass": true, "score": 8, "notes": "n",'
                           ' "classification": "none"}'}

    monkeypatch.setattr(eval_runner, "_model_text", driver_then_judge)
    rt = _FakeRuntime(["answer one", "answer two"])
    store = EvalStore(tmp_path / "eval.db")
    case = _case(driver="adaptive")
    row = run(eval_runner.run_case(rt, case, store))
    assert [c[0] for c in rt.calls] == ["hi", "and for kids?"]
    # history threaded: second turn saw the first exchange
    hist = rt.calls[1][1]["history"]
    assert hist[0] == {"role": "user", "content": "hi"}
    assert row["passed"] in (True, 1)
    store.close()


def test_run_suite_cost_cap(tmp_path, monkeypatch):
    async def pricey_judge(cfg, alias, messages, **kw):
        return {"status": "ok", "model_name": "j", "cost_usd": 99.0,
                "tokens": 1, "error": None,
                "content": '{"pass": true, "score": 8, "notes": "n",'
                           ' "classification": "none"}'}
    monkeypatch.setattr(eval_runner, "_model_text", pricey_judge)
    rt = _FakeRuntime(["a", "b"])
    rt.config["eval"]["suite_max_cost_usd"] = 0.01
    store = EvalStore(tmp_path / "eval.db")
    cases = [_case(id="c1"), _case(id="c2")]
    summary = run(eval_runner.run_suite(rt, cases, store))
    assert summary["ran"] == 1 and summary["results"][1]["skipped"]
    store.close()


# ---- tools ----------------------------------------------------------------------

def test_eval_run_tool_without_runtime(ctx):
    from tools.eval.run import EvalRun
    eval_runner.set_runtime(None)
    try:
        res = run(EvalRun().execute({"id": "x"}, ctx()))
        assert res.status == "error" and "web server" in res.error
    finally:
        eval_runner.set_runtime(None)


def test_eval_list_and_report(ctx, tmp_path, monkeypatch):
    from runtime import paths
    from tools.eval.run import EvalList, EvalReport
    monkeypatch.setattr(paths, "EVAL_DB", tmp_path / "eval.db")
    monkeypatch.setattr(paths, "CUSTOM_EVALS_DIR", tmp_path / "nope")
    monkeypatch.setattr(paths, "HOME", _REPO)
    res = run(EvalList().execute({}, ctx()))
    assert res.status == "ok" and res.result["count"] >= 14
    ids = {c["id"] for c in res.result["cases"]}
    assert "web-freshness" in ids
    rep = run(EvalReport().execute({}, ctx()))
    assert rep.status == "ok" and rep.result["total_runs"] == 0
