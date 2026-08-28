"""Eval harness core: case loading, store, expectations, runner (fake runtime).

No network, no real models: the runner is driven against a stub AgentRuntime
and a stubbed _model_text (judge/driver).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import run

from runtime import eval_runner, paths
from runtime.eval_cases import EvalCase, load_cases, parse_case, validate_case_dict
from runtime.eval_store import EvalStore

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
    # same classification+cause+fix → upsert while pending (same row), even
    # for another test; frozen once decided
    dup = s.add_proposal(test_id="t2", result_id=2, classification="config",
                         what="w2", cause="C", fix="F")
    assert dup and dup["id"] == p1["id"]
    s.set_proposal_status(p1["id"], "accepted")
    assert s.add_proposal(test_id="t2", result_id=2, classification="config",
                          what="w2", cause="C", fix="F") is None
    assert s.proposals("accepted")[0]["id"] == p1["id"]
    assert s.proposals("pending") == []
    with pytest.raises(ValueError):
        s.set_proposal_status(p1["id"], "bogus")
    s.close()


def test_store_proposals_merge_pending_siblings(tmp_path):
    """The judge paraphrases across runs, defeating exact-key dedup: one open
    item per (case, class) — a fresh proposal replaces older pending
    siblings; decided rows and other cases/classes are untouched."""
    s = EvalStore(tmp_path / "eval.db")
    old = s.add_proposal(test_id="t", result_id=1, classification="prompt-tweak",
                         what="w1", cause="c1", fix="strengthen the directive")
    other_cls = s.add_proposal(test_id="t", result_id=1, classification="config",
                               what="w", cause="c", fix="raise the cap")
    other_case = s.add_proposal(test_id="t2", result_id=1,
                                classification="prompt-tweak",
                                what="w", cause="c", fix="same fix, other case")
    new = s.add_proposal(test_id="t", result_id=2, classification="prompt-tweak",
                         what="w2", cause="c2", fix="make the directive a hard rule")
    pending = {p["id"] for p in s.proposals("pending")}
    assert pending == {other_cls["id"], other_case["id"], new["id"]}
    assert old["id"] not in pending
    # a decided sibling is frozen, not merged away
    s.set_proposal_status(new["id"], "rejected")
    again = s.add_proposal(test_id="t", result_id=3, classification="prompt-tweak",
                           what="w3", cause="c3", fix="third wording")
    assert again and again["id"] not in (old["id"], new["id"])
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


def test_check_expectations_unavailable_tool_message():
    """A must_use tool excluded from the eval toolset made the rubric
    impossible — the failure must point at the case/toolset, not the agent
    (a state-blind judge turned exactly this into bogus prompt-tweaks)."""
    case = EvalCase(id="w", name="w", turns=["hi"],
                    expect={"must_use_tools": ["fs.write"]},
                    judge_rubric="r")
    plain = eval_runner.check_expectations(case, [_turn()],
                                           available={"fs.read", "fs.write"})
    assert plain == ["expected tool 'fs.write' was never called"]
    unavail = eval_runner.check_expectations(case, [_turn()],
                                             available={"fs.read"})
    assert len(unavail) == 1
    assert "not available" in unavail[0] and "not the prompt" in unavail[0]


def test_check_expectations_must_use_any():
    """OR-semantics for execution tools: code-task accepts code.run OR
    code.execute OR test.run — requiring all three would false-fail every
    legitimate single-tool run."""
    case = EvalCase(id="a", name="a", turns=["hi"],
                    expect={"must_use_any_tools": ["code.run", "code.execute"]},
                    judge_rubric="r")
    assert eval_runner.check_expectations(
        case, [_turn(tools=["code.execute"])]) == []
    assert eval_runner.check_expectations(
        case, [_turn(tools=["code.run"])]) == []
    fails = eval_runner.check_expectations(case, [_turn(tools=["fs.read"])],
                                           available={"code.run", "fs.read"})
    assert len(fails) == 1 and "none of the expected tools" in fails[0]
    # none of the alternatives even available → case/toolset problem wording
    unavail = eval_runner.check_expectations(case, [_turn(tools=["fs.read"])],
                                             available={"fs.read"})
    assert len(unavail) == 1
    assert "none was available" in unavail[0] and "not the prompt" in unavail[0]


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


class _ListRegistry:
    def __init__(self, tools):
        self._tools = tools

    def all(self):
        return self._tools


def _mini_runtime(confirm_on=True):
    """Registry with gated/remote probes for _unattended_tools tests."""
    tools = [_FakeTool("fs.read"), _FakeTool("fs.write", gated=True),
             _FakeTool("fs.edit", gated=True),
             _FakeTool("fs.delete", gated=True),
             _FakeTool("llm.call"), _FakeTool("eval.run")]
    rt = _FakeRuntime(["ok"])
    rt.registry = _ListRegistry(tools)
    rt.config = {"confirmation": {"enabled": confirm_on},
                 "privacy": {"remote_llm_tools": ["llm.call"]},
                 "eval": {}, "costs": {}}
    return rt


def test_unattended_tools_confined_gated_and_remote():
    # fs.write/fs.edit are gated but sandbox-confined → included; other
    # gated tools and eval.run stay out; llm.call stays in while the
    # confirmation gate can deny it...
    got = eval_runner._unattended_tools(_mini_runtime(confirm_on=True), None)
    assert set(got) == {"fs.read", "fs.write", "fs.edit", "llm.call"}
    # ...and drops out when the gate is globally disabled (it would
    # auto-approve cloud calls carrying eval secrets).
    got = eval_runner._unattended_tools(_mini_runtime(confirm_on=False), None)
    assert "llm.call" not in got and "fs.write" in got


def test_eval_confirm_approves_confined_only():
    prov = eval_runner._EvalConfirm()
    assert run(prov.confirm("r", "fs.write", {}, None)) is True
    assert run(prov.confirm("r", "fs.edit", {}, None)) is True
    assert run(prov.confirm("r", "llm.call", {}, None)) is False
    assert run(prov.confirm("r", "ops.run", {}, None)) is False


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
    # unattended wiring: non-confined gated tools + eval.run excluded;
    # the confirm provider approves sandbox-confined writes only
    kwargs = rt.calls[0][1]
    assert "fs.delete" not in kwargs["tools"]
    assert "eval.run" not in kwargs["tools"]
    prov = kwargs["confirm_provider"]
    assert run(prov.confirm("r", "fs.write", {}, None)) is True
    assert run(prov.confirm("r", "llm.call", {}, None)) is False
    assert kwargs["owner"] == "_eval"
    assert kwargs["budget_overrides"]["max_iterations"] == 0
    # wall clock is the one ceiling eval runs keep: the $ cap can't fire on
    # $0.00 local brains, so without it a stuck case blocks the whole suite
    assert kwargs["budget_overrides"]["max_wall_clock_s"] == 1800
    # persistent stores redirected into the per-case sandbox (audit S2)
    patch = kwargs["run_overrides"]["tools_patch"]
    assert patch["memory"]["db_path"].endswith("memory.db")
    assert "/srv/" not in patch["memory"]["db_path"]
    assert patch["rag"]["db_path"].endswith("rag.db")
    # recorded
    assert store.results("demo")
    store.close()


def test_run_case_wall_clock_configurable(tmp_path, monkeypatch):
    """eval.turn_wall_clock_s flows into the run budget; 0 = the old
    unlimited behavior for those who want it."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["all prices are 2026"])
    rt.config["eval"]["turn_wall_clock_s"] = 0
    store = EvalStore(tmp_path / "eval.db")
    run(eval_runner.run_case(rt, _case(), store))
    assert rt.calls[0][1]["budget_overrides"]["max_wall_clock_s"] == 0
    store.close()


def test_run_case_budget_override_wins(tmp_path, monkeypatch):
    """A case's own budget.turn_wall_clock_s beats the global eval config —
    marathon benchmark cases carry their own cap (0 = unlimited still
    honored when set explicitly at case level)."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["all prices are 2026"])
    store = EvalStore(tmp_path / "eval.db")
    run(eval_runner.run_case(
        rt, _case(budget={"turn_wall_clock_s": 1200}), store))
    assert rt.calls[0][1]["budget_overrides"]["max_wall_clock_s"] == 1200
    rt2 = _FakeRuntime(["all prices are 2026"])
    run(eval_runner.run_case(
        rt2, _case(budget={"turn_wall_clock_s": 0}), store))
    assert rt2.calls[0][1]["budget_overrides"]["max_wall_clock_s"] == 0
    store.close()


def test_run_case_brain_variant_strips_delegation(tmp_path, monkeypatch):
    """harness:'brain' removes the delegation verbs (code.delegate /
    architect / agent.spawn) — the brain-only A/B against JayNet's model
    routing. 'full' (and the default) keeps them."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)

    class _Reg(_FakeRegistry):
        def all(self):
            return super().all() + [_FakeTool("code.delegate"),
                                    _FakeTool("architect"),
                                    _FakeTool("agent.spawn")]

    rt = _FakeRuntime(["ok"])
    rt.registry = _Reg()
    store = EvalStore(tmp_path / "eval.db")
    run(eval_runner.run_case(rt, _case(), store,
                             variant={"label": "v", "harness": "brain"}))
    tools = rt.calls[0][1]["tools"]
    for t in ("code.delegate", "architect", "agent.spawn"):
        assert t not in tools
    store.close()

    rt2 = _FakeRuntime(["ok"])
    rt2.registry = _Reg()
    store2 = EvalStore(tmp_path / "eval2.db")
    run(eval_runner.run_case(rt2, _case(), store2,
                             variant={"label": "v", "harness": "full"}))
    assert "code.delegate" in rt2.calls[0][1]["tools"]
    store2.close()


def test_switching_case_skips_in_brain_variant(tmp_path, monkeypatch):
    """The model-switching case (requires_tools: [code.delegate]) must run
    flawlessly in every variant: SKIP — never fail — under 'brain' (which
    strips the delegation verbs by design), and execute under 'full'."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)

    class _Reg(_FakeRegistry):
        def all(self):
            return super().all() + [_FakeTool("code.delegate")]

    case = _case(requires_tools=["code.delegate"], expect={})

    rt = _FakeRuntime(["ok"])
    rt.registry = _Reg()
    store = EvalStore(tmp_path / "eval.db")
    row = run(eval_runner.run_case(rt, case, store,
                                   variant={"label": "v", "harness": "brain"}))
    assert row["skipped"] is True
    assert "code.delegate" in row["note"]
    assert not rt.calls  # never reached the model
    store.close()

    rt2 = _FakeRuntime(["ok"])
    rt2.registry = _Reg()
    store2 = EvalStore(tmp_path / "eval2.db")
    row2 = run(eval_runner.run_case(rt2, case, store2,
                                    variant={"label": "v", "harness": "full"}))
    assert not row2.get("skipped")
    assert rt2.calls and "code.delegate" in rt2.calls[0][1]["tools"]
    store2.close()


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


def test_judge_sees_state_block(monkeypatch):
    """State-aware judging: the judge input carries the run's available
    tools, the live system prompt, targeted tool descriptions, and config —
    so it cannot propose tweaks for what the prompt already says or for
    tools the run never had."""
    seen = {}

    async def capture(cfg, alias, messages, **kw):
        seen["user"] = messages[-1]["content"]
        seen["max_tokens"] = kw.get("max_tokens")
        return {"status": "ok", "model_name": "j", "cost_usd": 0.0,
                "tokens": 1, "error": None,
                "content": '{"pass": true, "score": 8, "notes": "n",'
                           ' "classification": "none"}'}

    monkeypatch.setattr(eval_runner, "_model_text", capture)
    state = {"available_tools": ["fs.read", "fs.write"],
             "system_prompt": "LIVE-OVERLAY-PROMPT-TEXT",
             "tool_descriptions": {"fs.write": "Write content to a file."},
             "config": {"loop_guard": {"max_rejections": 6}}}
    out = run(eval_runner._judge({}, eval_runner.config({}), _case(),
                                 [_turn("fs.read(x)→ok", "done")],
                                 ["expected tool 'fs.write' was never called"],
                                 state))
    assert out["pass"] is True and out["error"] is None
    u = seen["user"]
    assert "AVAILABLE TOOLS" in u and "fs.write" in u
    assert "LIVE-OVERLAY-PROMPT-TEXT" in u
    assert "Write content to a file." in u
    assert "RELEVANT CONFIG" in u
    assert seen["max_tokens"] == eval_runner._JUDGE_MAX_TOKENS


def test_judge_retries_unparseable_json(monkeypatch):
    """skill-load lost a run to 'judge returned unparseable JSON' — one
    retry with a JSON-only nudge, cost accumulated from both calls."""
    calls = []

    async def flaky(cfg, alias, messages, **kw):
        calls.append(len(messages))
        content = ('verdict: {broken' if len(calls) == 1 else
                   '{"pass": false, "score": 3, "notes": "n",'
                   ' "classification": "none"}')
        return {"status": "ok", "model_name": "j", "cost_usd": 0.5,
                "tokens": 10, "error": None, "content": content}

    monkeypatch.setattr(eval_runner, "_model_text", flaky)
    out = run(eval_runner._judge({}, eval_runner.config({}), _case(),
                                 [_turn()], [], None))
    assert out["score"] == 3 and out["error"] is None
    assert len(calls) == 2 and calls[1] == 4     # retry appends the exchange
    assert out["cost_usd"] == 1.0 and out["tokens"] == 20


def test_judge_reports_truncation_distinctly(monkeypatch):
    """A reasoning judge that burns its token budget on thinking returns
    finish_reason 'length' with cut-off JSON — after the retry that must
    read as truncation (fix the budget), not 'unparseable' (fix the model)."""
    async def cut(cfg, alias, messages, **kw):
        return {"status": "ok", "model_name": "j", "cost_usd": 0.0,
                "tokens": 10, "error": None, "finish_reason": "length",
                "content": '{"pass": true, "score": 9, "notes": "long ver'}
    monkeypatch.setattr(eval_runner, "_model_text", cut)
    out = run(eval_runner._judge({}, eval_runner.config({}), _case(),
                                 [_turn()], [], None))
    assert out["error"] == "bad judge json"
    assert out["notes"].startswith("judge verdict truncated at the token cap")
    assert "content head:" in out["notes"]   # the cut-off blob is recorded


def test_judge_falls_back_on_garbage(monkeypatch):
    """HTTP-200 garbage never raises, so _model_text's alias fallback can't
    fire: after the retry fails, the judge tries the fallback alias
    explicitly (OpenRouter upstreams intermittently return junk for
    json_object calls — found live on glm-5.2)."""
    calls = []

    async def junk(cfg, alias, messages, **kw):
        calls.append(alias)
        if alias == eval_runner._FALLBACK_ALIAS:
            return {"status": "ok", "model_name": "local-27b", "cost_usd": 0.0,
                    "tokens": 5, "error": None,
                    "content": '{"pass": true, "score": 7, "notes": "ok",'
                               ' "classification": "none"}'}
        return {"status": "ok", "model_name": "glm-5.2", "cost_usd": 0.0,
                "tokens": 5, "error": None, "content": "I cannot grade this."}

    monkeypatch.setattr(eval_runner, "_model_text", junk)
    out = run(eval_runner._judge({}, eval_runner.config({}), _case(),
                                 [_turn()], [], None))
    assert out["pass"] is True and out["score"] == 7 and out["error"] is None
    assert out["judge_model"] == "local-27b"
    assert "graded by the fallback judge" in out["notes"]
    assert calls.count("glm-5.2") == 2 and calls.count(
        eval_runner._FALLBACK_ALIAS) == 1


def test_judge_state_shows_case_budget(monkeypatch):
    """The judge sees the case's own budget in RELEVANT CONFIG — without it
    it proposed global budget changes for per-case marathons (live: dozens
    of unactionable/dangerous config proposals)."""
    seen = {}

    async def capture(cfg, alias, messages, **kw):
        seen["user"] = messages[-1]["content"]
        return {"status": "ok", "model_name": "j", "cost_usd": 0.0,
                "tokens": 1, "error": None,
                "content": '{"pass": true, "score": 8, "notes": "n",'
                           ' "classification": "none"}'}

    monkeypatch.setattr(eval_runner, "_model_text", capture)
    state = {"config": {"case_budget": {"turn_wall_clock_s": 1200},
                        "budgets": {"max_wall_clock_s": 600}}}
    out = run(eval_runner._judge({}, eval_runner.config({}), _case(),
                                 [_turn()], [], state))
    assert out["pass"] is True
    assert '"case_budget": {"turn_wall_clock_s": 1200}' in seen["user"]
    # and the rules that keep budget/timeout proposals out of the inbox
    assert "case_budget" in eval_runner._JUDGE_SYSTEM
    assert "budgets.max_iterations" in eval_runner._JUDGE_SYSTEM
    assert "never propose global budget" in eval_runner._JUDGE_SYSTEM.lower()


def test_judge_sees_complete_tools_line(monkeypatch):
    """The trajectory display keeps only the most recent 14 tool entries, so
    a skill.load in iteration 1 of a long run is truncated away. The judge
    must still see that the tool ran — via the structural per-turn tools
    list — or it fails the case on a phantom "skill never loaded" (found
    live on skill-load/j-space-loop)."""
    seen = {}

    async def capture(cfg, alias, messages, **kw):
        seen["user"] = messages[-1]["content"]
        return {"status": "ok", "model_name": "j", "cost_usd": 0.0,
                "tokens": 1, "error": None,
                "content": '{"pass": true, "score": 8, "notes": "n",'
                           ' "classification": "none"}'}

    monkeypatch.setattr(eval_runner, "_model_text", capture)
    turn = _turn("fs.read(x)→ok; code.run(pytest)→ok", "done",
                 tools=["skill.load", "fs.read", "code.run"])
    out = run(eval_runner._judge({}, eval_runner.config({}), _case(),
                                 [turn], [], None))
    assert out["pass"] is True
    assert "tools called (3, complete): skill.load, fs.read, code.run" \
        in seen["user"]


def test_skill_loads_from_trace(tmp_path, monkeypatch):
    """Trace tool_result rows carry skill.load args — the source of skill
    names when the trajectory string has long since truncated them away."""
    import sqlite3
    db = tmp_path / "trace.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " run_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL,"
                " iteration INTEGER, payload_json TEXT)")
    rows = [
        ("r1", "tool_result", {"tool": "skill.load", "status": "ok",
                               "args": {"name": "tdd"}}),
        ("r1", "tool_result", {"tool": "skill.load", "status": "error",
                               "args": {"name": "nope"}}),
        ("r1", "tool_result", {"tool": "fs.read", "status": "ok",
                               "args": {"path": "x"}}),
        ("r2", "tool_result", {"tool": "skill.load", "status": "ok",
                               "args": {"name": "j-space"}}),
        ("other", "tool_result", {"tool": "skill.load", "status": "ok",
                                  "args": {"name": "unrelated"}}),
    ]
    for rid, kind, payload in rows:
        con.execute("INSERT INTO events (run_id, ts, kind, payload_json)"
                    " VALUES (?, 0, ?, ?)", (rid, kind, json.dumps(payload)))
    con.commit()
    con.close()
    monkeypatch.setattr(paths, "TRACE_DB", db)
    assert eval_runner._skill_loads_from_trace(["r1", "r2"]) == {"tdd",
                                                                 "j-space"}
    assert eval_runner._skill_loads_from_trace([]) == set()
    monkeypatch.setattr(paths, "TRACE_DB", tmp_path / "missing.db")
    assert eval_runner._skill_loads_from_trace(["r1"]) == set()


def test_loaded_skill_bodies_extra_names(tmp_path, monkeypatch):
    """extra_names (trace-derived) finds bodies the truncated trajectory no
    longer shows."""
    monkeypatch.setattr(paths, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(paths, "CUSTOM_SKILLS_DIR", tmp_path / "custom")
    d = tmp_path / "skills" / "tdd"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: tdd\ndescription: x\n---\nWRITE TESTS FIRST",
        encoding="utf-8")
    bodies = eval_runner._loaded_skill_bodies(
        [{"trajectory": "fs.read(x)→ok"}], extra_names={"tdd"})
    assert bodies == {"tdd": "WRITE TESTS FIRST"}


def test_judge_unparseable_records_content_head(monkeypatch):
    """When every attempt fails, the row records what the judge actually
    returned — the next 'unparseable' is diagnosable from the admin UI."""
    async def junk(cfg, alias, messages, **kw):
        return {"status": "ok", "model_name": "j", "cost_usd": 0.0,
                "tokens": 5, "error": None, "content": "upstream HTML error page"}
    monkeypatch.setattr(eval_runner, "_model_text", junk)
    out = run(eval_runner._judge({}, eval_runner.config({}), _case(),
                                 [_turn()], [], None))
    assert out["error"] == "bad judge json"
    assert "unparseable JSON" in out["notes"]
    assert "upstream HTML error page" in out["notes"]


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
    assert summary["cancelled"] is False
    store.close()


def test_run_suite_should_stop_cancels_between_cases(tmp_path, monkeypatch):
    """Admin cancel: the case in flight finishes and is recorded; every
    later case is skipped-cancelled and the summary says cancelled."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["a", "b", "c"])
    store = EvalStore(tmp_path / "eval.db")
    cases = [_case(id="c1"), _case(id="c2"), _case(id="c3")]
    checks = {"n": 0}

    def stop():
        checks["n"] += 1
        return checks["n"] > 1          # first case runs, then the flag is set

    summary = run(eval_runner.run_suite(rt, cases, store, should_stop=stop))
    assert summary["cancelled"] is True
    assert summary["ran"] == 1
    assert all(r.get("skipped") for r in summary["results"][1:])
    assert "cancelled" in summary["results"][1]["note"]
    # the finished case was still recorded
    assert store.results("c1") and not store.results("c2")
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


def test_proposal_upsert_refreshes_pending_only(tmp_path):
    """Audit A1: a duplicate cause+fix refreshes target/proposed_content
    while pending; accepted/rejected stay frozen."""
    s = EvalStore(tmp_path / "eval.db")
    p1 = s.add_proposal(test_id="t", result_id=1, classification="tool-description",
                        target="fs.read", proposed_content="v1",
                        what="w", cause="c", fix="f")
    assert p1 and p1["proposed_content"] == "v1"
    p2 = s.add_proposal(test_id="t", result_id=2, classification="tool-description",
                        target="fs.read", proposed_content="v2-better",
                        what="w", cause="c", fix="f")
    assert p2 and p2["id"] == p1["id"]
    assert p2["proposed_content"] == "v2-better"
    s.set_proposal_status(p1["id"], "accepted")
    assert s.add_proposal(test_id="t", result_id=3,
                          classification="tool-description", target="fs.read",
                          proposed_content="v3", what="w", cause="c",
                          fix="f") is None
    assert s.get_proposal(p1["id"])["proposed_content"] == "v2-better"
    s.close()


def test_loaded_skill_bodies_honours_skills_dir(tmp_path, monkeypatch):
    """Audit A3: a skills.dir override is used over the default root."""
    other = tmp_path / "elsewhere" / "tdd"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text(
        "---\nname: tdd\ndescription: x\n---\nCUSTOM LOCATION BODY",
        encoding="utf-8")
    monkeypatch.setattr(paths, "SKILLS_DIR", tmp_path / "empty")
    turns = [{"trajectory": "skill.load(tdd)→ok"}]
    bodies = eval_runner._loaded_skill_bodies(turns,
                                              skills_dir=tmp_path / "elsewhere")
    assert bodies == {"tdd": "CUSTOM LOCATION BODY"}


def test_run_case_variant(tmp_path, monkeypatch):
    """Benchmark variants: model + sampling reach runtime.run, the label is
    recorded as the result's brain."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["answer"])
    store = EvalStore(tmp_path / "eval.db")
    variant = {"label": "brainA-t0", "model": "local-specialist",
               "sampling": {"temperature": 0, "seed": 42}}
    row = run(eval_runner.run_case(rt, _case(), store, variant=variant))
    kwargs = rt.calls[0][1]
    assert kwargs["model"] == "local-specialist"
    assert kwargs["run_overrides"]["sampling"] == {"temperature": 0, "seed": 42}
    assert kwargs["run_overrides"]["tools_patch"]        # sandboxing intact
    assert row["brain"] == "brainA-t0"
    assert store.results("demo")[0]["brain"] == "brainA-t0"
    # no variant: model stays the runtime default, no sampling override
    rt2 = _FakeRuntime(["answer"])
    run(eval_runner.run_case(rt2, _case(), store))
    kw2 = rt2.calls[0][1]
    assert kw2["model"] is None
    assert "sampling" not in kw2["run_overrides"]
    store.close()


# ---- requires_tools + project fixtures -----------------------------------------


def test_validate_requires_tools_and_project():
    import yaml
    base = "name: x\nturns: [{user: hi}]\njudge_rubric: r\n"
    ok = yaml.safe_load(base + "requires_tools: [graph.query]\n"
                        "project:\n  graph: true\n  files:\n    a.py: 'x = 1'\n")
    assert validate_case_dict("demo", ok) == []
    # seed_code alone (no literal files) is a valid fixture too
    assert validate_case_dict("demo", yaml.safe_load(
        base + "project:\n  seed_code: 'open(\"big.txt\", \"w\").write(\"x\")'\n")) == []
    for extra, needle in [
        ("requires_tools: graph.query", "requires_tools must be a list"),
        ("project: [1, 2]", "project must be a mapping"),
        ("project: {bogus: 1}", "unknown project key"),
        ("project: {files: {}}", "at least one file"),
        ("project: {files: {'/abs.py': 'x'}}", "relative path"),
        ("project: {files: {'../up.py': 'x'}}", "relative path"),
        ("project: {files: {'a.py': 42}}", "must be a string"),
        ("project: {seed_code: 42}", "seed_code must be a string"),
        ("project: {files: {'a.py': 'x'}, graph: 'soon'}",
         "graph must be a boolean"),
    ]:
        errors = validate_case_dict("demo", yaml.safe_load(base + extra))
        assert any(needle in e for e in errors), (extra, errors)


def test_parse_case_requires_tools_and_project():
    c = parse_case("demo", _VALID +
                   "requires_tools: [graph.query]\n"
                   "project:\n  graph: true\n  files:\n    a.py: 'x = 1'\n",
                   "builtin")
    assert c.requires_tools == ["graph.query"]
    assert c.project["graph"] is True
    assert c.project["files"]["a.py"] == "x = 1"
    assert c.to_dict()["requires_tools"] == ["graph.query"]


def test_patch_run_config_sections():
    from runtime.loop import _patch_run_config
    base = {"tools": {"memory": {"db_path": "/real/m.db"}},
            "web": {"projects_dir": "/real/projects", "port": 8071}}
    patched = _patch_run_config(
        base, None, {"web": {"projects_dir": "/tmp/sbx/projects"},
                     "tools": {"sneaky": True},      # tools: via tools_patch only
                     "junk": "not-a-dict"})
    assert patched["web"]["projects_dir"] == "/tmp/sbx/projects"
    assert patched["web"]["port"] == 8071
    assert "sneaky" not in patched["tools"]
    assert "junk" not in patched
    assert base["web"]["projects_dir"] == "/real/projects"
    assert _patch_run_config(base, None, None) is base


def test_run_case_skips_when_required_tool_missing(tmp_path, monkeypatch):
    """requires_tools: an install without the plugin skips, never fails —
    and the case never runs."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["ok"])
    store = EvalStore(tmp_path / "eval.db")
    row = run(eval_runner.run_case(rt, _case(requires_tools=["graph.query"]),
                                   store))
    assert row["skipped"] is True
    assert "graph.query" in row["note"]
    assert rt.calls == []
    store.close()


class _ProjectProbeRuntime(_FakeRuntime):
    async def run(self, message, **kwargs):
        # The fixture must be in place by turn 1: files under work_root,
        # project.json next to it.
        wr = Path(kwargs["work_root"])
        self.fixture_ok = ((wr / "models.py").is_file()
                           and (wr / "pkg" / "svc.py").is_file()
                           and (wr.parent / "project.json").is_file())
        return await super().run(message, **kwargs)


def test_run_case_project_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    # A plugin hint via the augment_project_context hook must reach turn 1
    # (mirrors the web layer's project prefix).
    from runtime import hooks
    hooks.clear()
    hooks.register("augment_project_context",
                   lambda owner, pid, meta, root: "[Project graph] stub hint")
    try:
        rt = _ProjectProbeRuntime(["service.py breaks"])
        store = EvalStore(tmp_path / "eval.db")
        case = _case(project={"files": {"models.py": "class User: ...",
                                        "pkg/svc.py": "import models"}})
        row = run(eval_runner.run_case(rt, case, store))
        kwargs = rt.calls[0][1]
        assert kwargs["project_id"] == "eval-demo"
        assert kwargs["work_root"].endswith("files")
        patch = kwargs["run_overrides"]["config_patch"]
        assert patch["web"]["projects_dir"].endswith("projects")
        assert "/srv/" not in patch["web"]["projects_dir"]   # sandboxed, not real
        assert rt.fixture_ok
        # Turn 1 carries the web-style project context: banner, file tree,
        # and the hook's hint line.
        msg = rt.calls[0][0]
        assert msg.startswith("[Project: Demo]")
        assert "models.py" in msg and "pkg/svc.py" in msg
        assert "[Project graph] stub hint" in msg
        assert row["passed"]
        store.close()
    finally:
        hooks.clear()


def test_run_case_project_graph_prebuild_missing_cli(tmp_path, monkeypatch):
    """project.graph without the graphify CLI → clean skip, not a crash."""
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["ok"])
    store = EvalStore(tmp_path / "eval.db")
    case = _case(project={"graph": True, "files": {"a.py": "x = 1"}})
    row = run(eval_runner.run_case(rt, case, store))
    assert row["skipped"] is True
    assert "graphify CLI not installed" in row["note"]
    assert rt.calls == []
    store.close()


def test_seed_project_seed_code(tmp_path):
    """seed_code runs with the fixture dir as cwd and generates the big file;
    a failing snippet raises (the suite runner records it as the case error)."""
    import pytest as _pt

    class _C:
        id = "seeded"
        name = "Seeded"
        project = {"seed_code": "open('big.txt', 'w').write('x' * 5000)"}
    pid, wr, patch = eval_runner._seed_project(str(tmp_path), _C)
    assert pid == "eval-seeded"
    assert (Path(wr) / "big.txt").read_text() == "x" * 5000
    assert (Path(wr).parent / "project.json").is_file()
    assert patch["web"]["projects_dir"].endswith("projects")

    class _Bad:
        id = "badseed"
        name = "Bad"
        project = {"seed_code": "raise SystemExit('boom-details')"}
    with _pt.raises(RuntimeError, match="boom-details"):
        eval_runner._seed_project(str(tmp_path), _Bad)


# ---- benchmark grading: exact match + checker script --------------------------

def test_normalize_exact():
    n = eval_runner._normalize_exact
    assert n("The Answer is 1,000.0!") == n("answer is 1000")
    assert n("  New   York,  USA ") == "new york usa"
    assert n("42") == n("42.0")


def test_check_expectations_answer_exact_any():
    case = EvalCase(id="g", name="g", turns=["hi"],
                    expect={"answer_exact_any": ["1,000", "one thousand"]},
                    judge_rubric="r")
    # GAIA-style: normalization makes case/articles/punctuation/number
    # formatting irrelevant; the marker, last line and whole answer count.
    # A bare sentence is NOT a match (same as the GAIA scorer — the harness
    # must extract an answer, e.g. via the FINAL ANSWER marker).
    assert eval_runner.check_expectations(
        case, [_turn(answer="working...\nFINAL ANSWER: 1,000.")]) == []
    assert eval_runner.check_expectations(
        case, [_turn(answer="working...\nFINAL ANSWER: One Thousand")]) == []
    assert eval_runner.check_expectations(
        case, [_turn(answer="computed it above\n1,000")]) == []
    assert eval_runner.check_expectations(
        case, [_turn(answer="The answer is 1,000.")]) != []
    assert eval_runner.check_expectations(
        case, [_turn(answer="I think about 999")]) != []
    # earlier turns don't count — only the final answer
    assert eval_runner.check_expectations(
        case, [_turn(answer="1,000"), _turn(answer="999")]) != []


def test_validate_answer_exact_and_checker():
    base = {"name": "x", "turns": [{"user": "hi"}], "judge_rubric": "r"}
    ok = dict(base, expect={"answer_exact_any": ["42"],
                            "checker": "import sys; sys.exit(0)"})
    assert validate_case_dict("x", ok) == []
    errs = validate_case_dict("x", dict(base, expect={"answer_exact_any": "42"}))
    assert any("answer_exact_any" in e for e in errs)
    errs = validate_case_dict("x", dict(base, expect={"checker": 5}))
    assert any("checker" in e for e in errs)


def test_run_checker_pass_and_fail(tmp_path):
    (tmp_path / "data.txt").write_text("hello", encoding="utf-8")
    ok = ("import pathlib, sys; "
          "sys.exit(0 if pathlib.Path('data.txt').read_text() == 'hello' else 1)")
    assert eval_runner._run_checker(ok, tmp_path, [_turn(answer="hi")]) == []
    bad = "import sys; print('nope, wanted X'); sys.exit(1)"
    fails = eval_runner._run_checker(bad, tmp_path, [_turn(answer="hi")])
    assert len(fails) == 1 and "nope, wanted X" in fails[0]
    # EVAL_ANSWER carries the final answer into the grading script
    env = ("import os, sys; "
           "sys.exit(0 if os.environ['EVAL_ANSWER'] == '42' else 1)")
    assert eval_runner._run_checker(env, tmp_path,
                                    [_turn(answer="41"), _turn(answer="42")]
                                    ) == []


def test_run_case_checker_failure_blocks_pass(tmp_path, monkeypatch):
    """A failing checker is a deterministic check failure: judge pass is not
    enough, and the failure text reaches the stored row (and the judge)."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["some answer"])
    store = EvalStore(tmp_path / "eval.db")
    case = _case(expect={"checker": "import sys; print('checker says no'); "
                                    "sys.exit(1)"})
    row = run(eval_runner.run_case(rt, case, store))
    assert row["passed"] in (False, 0)
    assert any("checker says no" in f for f in row["check_failures"])
    store.close()


def test_run_case_checker_pass_allows_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    rt = _FakeRuntime(["42"])
    store = EvalStore(tmp_path / "eval.db")
    case = _case(expect={
        "checker": ("import os, sys; "
                    "sys.exit(0 if os.environ['EVAL_ANSWER'] == '42' else 1)")})
    row = run(eval_runner.run_case(rt, case, store))
    assert row["passed"] in (True, 1)
    store.close()


# ---- container cases (podman) -------------------------------------------------

def test_validate_container_block():
    import yaml
    base = "name: x\nturns: [{user: hi}]\njudge_rubric: r\n"
    ok = yaml.safe_load(base + "container: {image: benchlab-tb-x-a1b2c3}\n")
    assert validate_case_dict("demo", ok) == []
    ok2 = yaml.safe_load(base + "container:\n  image: reg.local/ns/img:1.2\n"
                               "  workdir: /work\n")
    assert validate_case_dict("demo", ok2) == []
    for extra, needle in [
        ("container: [1]", "container must be a mapping"),
        ("container: {}", "container.image is required"),
        ("container: {workdir: /app}", "container.image is required"),
        ("container: {image: ''}", "container.image is required"),
        ("container: {image: 'bad tag!'}", "container.image is required"),
        ("container: {image: ok, workdir: rel/dir}", "absolute path"),
        ("container: {image: ok, network: 1}", "container.network must be a boolean"),
        ("container: {image: ok, bogus: 1}", "unknown container key 'bogus'"),
        ("budget: [1]", "budget must be a mapping"),
        ("budget: {bogus: 1}", "unknown budget key 'bogus'"),
        ("budget: {turn_wall_clock_s: -5}", "turn_wall_clock_s must be an int"),
        ("budget: {turn_wall_clock_s: true}", "turn_wall_clock_s must be an int"),
        ("bogus_top: 1", "unknown case key 'bogus_top'"),
    ]:
        errors = validate_case_dict("demo", yaml.safe_load(base + extra))
        assert any(needle in e for e in errors), (extra, errors)
    # network + budget are valid when well-formed
    ok3 = yaml.safe_load(base + "container: {image: ok, network: true}\n"
                                "budget: {turn_wall_clock_s: 1200}\n")
    assert validate_case_dict("demo", ok3) == []


def test_parse_case_container_roundtrip():
    c = parse_case("demo", _VALID + "container: {image: img:1, workdir: /app}\n",
                   "builtin")
    assert c.container == {"image": "img:1", "workdir": "/app"}
    assert c.to_dict()["container"] == {"image": "img:1", "workdir": "/app"}
    # absent by default
    assert parse_case("demo", _VALID, "builtin").container == {}


class _FakePodman:
    """Records eval_runner._podman calls; replays scripted (rc, out)."""
    def __init__(self, image_exists=True, run_rc=0):
        self.calls = []
        self.image_exists = image_exists
        self.run_rc = run_rc

    def __call__(self, *args, timeout=60):
        self.calls.append(list(args))
        if args[:2] == ("image", "exists"):
            return (0, b"") if self.image_exists else (1, b"")
        if args[0] == "create":
            return (0, b"tmp-ctr\n")
        if args[0] == "run":
            return (self.run_rc, b"ctr-abc123\n" if self.run_rc == 0
                    else b"boom")
        return (0, b"")                      # cp / rm / stop


def _podman_on(monkeypatch, fake):
    monkeypatch.setattr(eval_runner, "_podman", fake)
    monkeypatch.setattr(eval_runner.shutil, "which",
                        lambda name: "/usr/bin/podman")


def test_run_case_container_lifecycle(tmp_path, monkeypatch):
    """Full container flow with a fake podman: image check → run → tools_patch
    injection → checker gets EVAL_CONTAINER_ID → stop in cleanup."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    fake = _FakePodman()
    _podman_on(monkeypatch, fake)
    rt = _FakeRuntime(["done"])
    store = EvalStore(tmp_path / "eval.db")
    checker = ("import os, sys; "
               "sys.exit(0 if os.environ.get('EVAL_CONTAINER_ID') "
               "== 'ctr-abc123' and os.environ.get('EVAL_CONTAINER_WORKDIR') "
               "== '/app' else 1)")
    case = _case(container={"image": "benchlab-tb-x-abc", "workdir": "/app"},
                 expect={"checker": checker})
    row = run(eval_runner.run_case(rt, case, store))
    assert row["passed"] in (True, 1), row.get("check_failures")
    # podman call sequence: exists → create/cp/rm (image /app materialized
    # into the work_root — the bind mount would hide it) → run → stop
    seq = [c[0] for c in fake.calls]
    assert seq == ["image", "create", "cp", "rm", "run", "stop"], fake.calls
    assert fake.calls[2][1].endswith(":/app/.")
    run_cmd = fake.calls[4]
    assert run_cmd[:2] == ["run", "-d"] and "--rm" in run_cmd
    assert "--network" in run_cmd and "none" in run_cmd
    vol = run_cmd[run_cmd.index("-v") + 1]
    assert vol.endswith(":/app:rw")
    assert run_cmd[-3:] == ["benchlab-tb-x-abc", "sleep", "infinity"]
    assert fake.calls[5] == ["stop", "ctr-abc123"]
    # code.execute routed into the container via run_overrides tools_patch
    patch = rt.calls[0][1]["run_overrides"]["tools_patch"]
    assert patch["code"]["container"] == {"id": "ctr-abc123",
                                          "workdir": "/app",
                                          "python": "python3"}
    store.close()


def test_run_case_container_network_opt_in(tmp_path, monkeypatch):
    """container.network: true drops --network none (download tasks, official
    TB posture); the default stays air-gapped."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    fake = _FakePodman()
    _podman_on(monkeypatch, fake)
    rt = _FakeRuntime(["done"])
    store = EvalStore(tmp_path / "eval.db")
    case = _case(container={"image": "img", "workdir": "/app",
                            "network": True})
    run(eval_runner.run_case(rt, case, store))
    run_cmd = [c for c in fake.calls if c[0] == "run"][0]
    assert "--network" not in run_cmd
    store.close()


def test_run_case_container_cleanup_on_error(tmp_path, monkeypatch):
    """A crashing turn still stops the container (the exception propagates to
    run_suite, which records the crash row)."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    fake = _FakePodman()
    _podman_on(monkeypatch, fake)

    class _CrashRuntime(_FakeRuntime):
        async def run(self, message, **kwargs):
            raise RuntimeError("boom")

    rt = _CrashRuntime(["x"])
    store = EvalStore(tmp_path / "eval.db")
    case = _case(container={"image": "img"})
    with pytest.raises(RuntimeError, match="boom"):
        run(eval_runner.run_case(rt, case, store))
    assert ["stop", "ctr-abc123"] in fake.calls
    store.close()


def test_run_case_container_preflight_skips(tmp_path, monkeypatch):
    """No podman → skip; image not built → skip pointing at bench.import.
    Capability gate, like requires_tools: never a failure, never a run."""
    monkeypatch.setattr(eval_runner, "_model_text", _judge_ok)
    store = EvalStore(tmp_path / "eval.db")
    case = _case(container={"image": "benchlab-tb-x-abc"})
    # podman binary missing
    monkeypatch.setattr(eval_runner.shutil, "which", lambda name: None)
    rt = _FakeRuntime(["x"])
    row = run(eval_runner.run_case(rt, case, store))
    assert row["skipped"] is True and "podman" in row["note"]
    assert rt.calls == []
    # image missing locally
    fake = _FakePodman(image_exists=False)
    _podman_on(monkeypatch, fake)
    row = run(eval_runner.run_case(rt, case, store))
    assert row["skipped"] is True and "bench.import" in row["note"]
    assert rt.calls == []
    assert fake.calls == [["image", "exists", "benchlab-tb-x-abc"]]
    # container fails to start
    fake2 = _FakePodman(run_rc=1)
    _podman_on(monkeypatch, fake2)
    row = run(eval_runner.run_case(rt, case, store))
    assert row["skipped"] is True and "failed to start" in row["note"]
    store.close()
