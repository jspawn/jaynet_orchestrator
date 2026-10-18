"""Contrastive harness experiments: validated overlays, isolation and the
Propose–Probe–Compose flow. No model backend or live artifact is touched."""
from __future__ import annotations

import copy
import json
import sqlite3

import pytest
from conftest import run

from runtime import eval_runner, paths
from runtime import harness_experiments as hx
from runtime.eval_cases import EvalCase
from runtime.eval_store import EvalStore
from runtime.loop import AgentRuntime
from runtime.selector import ToolSelector
from runtime.skills import skills_cache_clear
from runtime.tool_base import ToolContext
from runtime.tool_overrides import apply_schema
from tools.skill.load import SkillLoad


class _Tool:
    def __init__(self, name="web.search", description="original"):
        self.name = name
        self.description = description
        self.requires_confirmation = False


class _Registry:
    def __init__(self):
        self.tool = _Tool()

    def all(self):
        return [self.tool]

    def get(self, name):
        return self.tool if name == self.tool.name else None


class _Runtime:
    def __init__(self):
        self.config = {"eval": {}, "privacy": {}, "costs": {},
                       "architect": {"threshold": 3}}
        self.registry = _Registry()
        self.skills = {"coding": {"description": "write software"}}
        self.system_prompt = "BASE PROMPT"
        self.calls = []

    async def run(self, message, **kwargs):
        self.calls.append(kwargs)
        return {"run_id": "fake-run", "status": "ok", "answer": "done",
                "trajectory": "web.search(q)→ok", "tools_used": ["web.search"],
                "budget": {"cost_usd": 0, "tokens": {"total": 5}}}


def _case():
    return EvalCase(id="demo", name="Demo", tags=["smoke"], turns=["do it"],
                    judge_rubric="pass when correct")


def _overlay():
    return {
        "prompt_addition": "verify the result",
        "skill_addition": {"name": "coding", "content": "run the tests"},
        "tool_description": {"name": "web.search", "content": "search carefully"},
        "config": {"key": "architect.threshold", "value": 2},
    }


def test_validate_overlay_and_runtime_view_is_isolated():
    rt = _Runtime()
    original = copy.deepcopy(rt.config)
    overlay = hx.validate_overlay(_overlay(), rt, allow_empty=False)
    view = hx.runtime_view(rt, overlay)
    assert view is not rt and view.config is not rt.config
    assert view.config["architect"]["threshold"] == 2
    assert rt.config == original
    view._served_model_log.append({"requested": "a", "served": "b"})
    assert not hasattr(rt, "_served_model_log")
    with pytest.raises(ValueError, match="not whitelisted"):
        hx.validate_overlay({"config": {"key": "paths.home", "value": "/x"}}, rt)
    with pytest.raises(ValueError, match="unknown name"):
        hx.validate_overlay({"skill_addition": {"name": "ghost", "content": "x"}}, rt)
    with pytest.raises(ValueError, match="unknown fields"):
        hx.validate_overlay({"run_overrides": {}}, rt)


def test_schema_overlay_is_pure():
    schemas = [{"type": "function",
                "function": {"name": "web.search", "description": "old"}}]
    out = apply_schema(schemas, {"web.search": "new"})
    assert out[0]["function"]["description"] == "new"
    assert schemas[0]["function"]["description"] == "old"
    assert out[0] is not schemas[0]


def test_loop_applies_tool_description_to_model_schema_only():
    class LoopTool(_Tool):
        def to_openai_schema(self):
            return {"type": "function", "function": {
                "name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": {}}}}

    class LoopRegistry(_Registry):
        def __init__(self):
            self.tool = LoopTool()

        def openai_schemas(self, allowed=None):
            return [self.tool.to_openai_schema()]

    class Trace:
        def start_run(self, *args, **kwargs): pass
        def log(self, *args, **kwargs): pass
        def finish_run(self, *args, **kwargs): pass

    rt = AgentRuntime.__new__(AgentRuntime)
    rt.config = {"orchestrator": {"model": "local-orchestrator"},
                 "budgets": {"max_iterations": 2, "max_wall_clock_s": 60,
                             "max_cost_usd": 1, "max_total_tokens": 10000},
                 "privacy": {}}
    rt.registry = LoopRegistry()
    rt.selector = ToolSelector(rt.registry, rt.config)
    rt.trace = Trace()
    rt.system_prompt, rt.skill_catalog = "test", ""
    rt.litellm_base, rt.model = "http://x:4000", "local-orchestrator"
    rt.cost_table, rt.brain_info = {}, {}
    rt.vision_enabled = False
    rt._local_concurrency, rt._local_aliases = {}, frozenset()
    rt._model_sems, rt._poll_safe = {}, set()
    seen = []

    async def fake_turn(messages, tools_schema, **kwargs):
        seen.append(tools_schema)
        return {"message": {"role": "assistant", "content": "done"}, "usage": {}}

    rt._model_turn = fake_turn
    result = run(rt.run(
        "hello", tools=["web.search"],
        run_overrides={"tool_descriptions": {"web.search": "NEW"}}))
    assert result["status"] == "ok"
    assert seen[0][0]["function"]["description"] == "NEW"
    assert rt.registry.tool.description == "original"


def test_skill_load_appends_only_the_run_scoped_addition(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    custom = tmp_path / "custom"
    skill = skills / "coding"
    skill.mkdir(parents=True)
    body = "---\nname: coding\ndescription: code\n---\nORIGINAL"
    (skill / "SKILL.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr(paths, "SKILLS_DIR", skills)
    monkeypatch.setattr(paths, "CUSTOM_SKILLS_DIR", custom)
    skills_cache_clear()
    ctx = ToolContext(request_id="r", config={"skills": {"dir": str(skills)}},
                      budget=None, skill_additions={"coding": "CANDIDATE"})
    result = run(SkillLoad().execute({"name": "coding"}, ctx))
    assert result.status == "ok"
    assert result.result["instructions"].endswith("— Experiment addition —\nCANDIDATE")
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == body
    skills_cache_clear()


def test_run_case_overlay_reaches_run_and_judge(tmp_path, monkeypatch):
    rt = _Runtime()
    seen = {}

    async def judge(cfg, ecfg, case, transcript, failures, state):
        seen["state"] = state
        return {"pass": True, "score": 9, "notes": "fine",
                "judge_model": "j", "cost_usd": 0, "tokens": 1, "error": None,
                "classification": "none", "target": "", "proposed_content": "",
                "what": "", "cause": "", "fix": ""}

    monkeypatch.setattr(eval_runner, "_judge", judge)
    store = EvalStore(tmp_path / "eval.db")
    overlay = {"prompt_addition": "CANDIDATE",
               "tool_description": {"name": "web.search", "content": "REPLACEMENT"}}
    row = run(eval_runner.run_case(
        rt, _case(), store, record=False,
        variant={"label": "experiment", "experiment_overlay": overlay}))
    assert row["passed"] is True
    kwargs = rt.calls[0]
    assert kwargs["extra_system"] == "CANDIDATE"
    assert kwargs["run_overrides"]["tool_descriptions"] == {
        "web.search": "REPLACEMENT"}
    assert seen["state"]["system_prompt"].endswith("CANDIDATE")
    assert seen["state"]["tool_descriptions"]["web.search"] == "REPLACEMENT"
    assert rt.system_prompt == "BASE PROMPT"
    assert rt.registry.tool.description == "original"
    assert store.results() == [] and store.proposals() == []
    store.close()


def _editor_result(content):
    return {"status": "ok", "content": content, "model_name": "editor",
            "served_model": "editor", "finish_reason": "stop",
            "cost_usd": 0.001, "tokens": 20, "error": None}


def test_full_experiment_and_gated_proposals(tmp_path, monkeypatch):
    rt = _Runtime()
    original_config = copy.deepcopy(rt.config)
    store = EvalStore(tmp_path / "eval.db")
    exp = hx.create_experiment(store, _case(), reps=2)
    calls = []

    async def fake_case(view, case, store, *, disabled_tools=None, record=True,
                        variant=None):
        label = variant["label"].rsplit("-", 1)[-1]
        calls.append((label, record, copy.deepcopy(variant["experiment_overlay"])))
        passed = label != "baseline"
        return {"test_id": case.id, "passed": passed, "score": 9 if passed else 3,
                "judge_notes": label, "judge_model": "j", "cost_usd": 0.001,
                "tokens": 10, "elapsed_s": 1, "status": "ok", "run_ids": [label],
                "check_failures": [], "judge_error": None,
                "transcript": [{"user": "do it", "status": "ok", "answer": label,
                                "trajectory": "web.search(q)→ok",
                                "tools": ["web.search"]}]}

    async def editor(cfg, alias, messages, **kwargs):
        system = messages[0]["content"]
        if "Propose TWO" in system:
            return _editor_result(
                '{"invariant":{"rationale":"baseline skips verification",'
                '"overlay":{"prompt_addition":"verify before final"}},'
                '"domain":{"rationale":"search was imprecise",'
                '"overlay":{"tool_description":{"name":"web.search",'
                '"content":"search with exact entities"}}}}')
        assert "Compose a successor" in system
        return _editor_result(
            '{"rationale":"both probed edits addressed the observed failures",'
            '"overlay":{"prompt_addition":"verify before final",'
            '"tool_description":{"name":"web.search",'
            '"content":"search with exact entities"}}}')

    monkeypatch.setattr(eval_runner, "run_case", fake_case)
    monkeypatch.setattr(eval_runner, "_model_text", editor)
    result = run(hx.run_experiment(rt, _case(), store, exp["id"]))
    assert result["status"] == "done"
    assert [c[0] for c in calls] == [
        "baseline", "baseline", "invariant", "invariant",
        "domain", "domain", "composed", "composed"]
    assert all(c[1] is False for c in calls)
    saved = store.get_harness_experiment(exp["id"])
    assert saved["baseline"]["summary"]["pass_rate"] == 0
    assert saved["composed"]["summary"]["pass_rate"] == 1
    assert saved["lesson"]["evidence"]["reps"] == 2
    assert saved["total"]["tokens"] == 120  # 8 arms × 10 + 2 editors × 20
    assert rt.config == original_config

    proposals = hx.create_proposals(store, exp["id"])
    assert {p["classification"] for p in proposals} == {
        "prompt-tweak", "tool-description"}
    assert all(p["status"] == "pending" for p in proposals)
    assert len(store.get_harness_experiment(exp["id"])["proposal_ids"]) == 2
    with pytest.raises(ValueError, match="already created"):
        hx.create_proposals(store, exp["id"])
    store.close()


def test_composed_overlay_cannot_introduce_unprobed_target():
    candidates = {"invariant": {"overlay": {"prompt_addition": "x"}}}
    overlay = {"tool_description": {"name": "web.search", "content": "new"}}
    with pytest.raises(ValueError, match="was not probed"):
        hx._assert_previously_probed(overlay, candidates)


def test_cancellation_preserves_baseline_evidence(tmp_path, monkeypatch):
    rt = _Runtime()
    store = EvalStore(tmp_path / "eval.db")
    exp = hx.create_experiment(store, _case(), reps=1)
    calls = []

    async def fake_case(*args, **kwargs):
        calls.append(1)
        return {"test_id": "demo", "passed": False, "score": 2,
                "judge_notes": "baseline", "cost_usd": 0, "tokens": 1,
                "elapsed_s": 1, "status": "ok", "transcript": []}

    monkeypatch.setattr(eval_runner, "run_case", fake_case)
    states = iter([False, True])
    result = run(hx.run_experiment(
        rt, _case(), store, exp["id"], should_stop=lambda: next(states, True)))
    assert result["status"] == "cancelled"
    saved = store.get_harness_experiment(exp["id"])
    assert len(saved["baseline"]["runs"]) == 1
    assert len(calls) == 1
    store.close()


def _measured_row(label="baseline", **changes):
    row = {"test_id": "demo", "passed": True, "score": 9,
           "judge_notes": label, "judge_model": "j", "cost_usd": 0.001,
           "tokens": 10, "elapsed_s": 1, "status": "ok", "run_ids": [label],
           "check_failures": [], "judge_error": None, "skipped": False,
           "transcript": [{"user": "do it", "status": "ok", "answer": label,
                           "trajectory": "web.search(q)→ok",
                           "tools": ["web.search"]}]}
    row.update(changes)
    return row


def test_experiment_config_validation_is_typed_and_scoped():
    rt = _Runtime()
    assert hx.EXPERIMENT_CONFIG_WHITELIST == frozenset({
        "architect.threshold", "loop_guard.max_rejections", "eval.max_cost_usd"})
    assert hx.CONFIG_WHITELIST == frozenset({
        "budgets.max_iterations", "budgets.max_wall_clock_s",
        "budgets.max_cost_usd", "budgets.max_total_tokens",
        "loop_guard.max_rejections", "architect.threshold",
        "eval.max_cost_usd", "eval.suite_max_cost_usd",
        "eval.adaptive_max_turns"})
    for key, raw, expected in [
            ("architect.threshold", "2", 2),
            ("loop_guard.max_rejections", 0.0, 0),
            ("eval.max_cost_usd", "0.25", 0.25)]:
        out = hx.validate_overlay(
            {"config": {"key": key, "value": raw}}, rt)["config"]["value"]
        assert out == expected and type(out) is type(expected)
    bad_values = [
        ("eval.max_cost_usd", "nan"), ("eval.max_cost_usd", float("inf")),
        ("eval.max_cost_usd", True), ("eval.max_cost_usd", 0),
        ("eval.max_cost_usd", -0.1), ("architect.threshold", False),
        ("architect.threshold", 1.5), ("architect.threshold", 5),
        ("loop_guard.max_rejections", "-inf"),
        ("loop_guard.max_rejections", -1),
        ("loop_guard.max_rejections", 2.5),
    ]
    for key, value in bad_values:
        with pytest.raises(ValueError):
            hx.validate_overlay({"config": {"key": key, "value": value}}, rt)
    for key in ("budgets.max_iterations", "eval.suite_max_cost_usd",
                "eval.adaptive_max_turns"):
        with pytest.raises(ValueError, match="not whitelisted for experiments"):
            hx.validate_overlay({"config": {"key": key, "value": 1}}, rt)


@pytest.mark.parametrize("content", [
    "not json",
    json.dumps({
        "invariant": {"rationale": "invalid numeric overlay", "overlay": {
            "config": {"key": "eval.max_cost_usd", "value": "nan"}}},
        "domain": {"rationale": "otherwise valid", "overlay": {
            "prompt_addition": "verify"}}}),
])
def test_propose_usage_is_recorded_on_malformed_or_invalid_output(
        tmp_path, monkeypatch, content):
    rt = _Runtime()
    store = EvalStore(tmp_path / "eval.db")
    exp = hx.create_experiment(store, _case(), reps=1)

    async def fake_case(view, case, store, **kwargs):
        return _measured_row()

    async def editor(cfg, alias, messages, **kwargs):
        return _editor_result(content)

    monkeypatch.setattr(eval_runner, "run_case", fake_case)
    monkeypatch.setattr(eval_runner, "_model_text", editor)
    result = run(hx.run_experiment(rt, _case(), store, exp["id"]))
    assert result["status"] == "error"
    saved = store.get_harness_experiment(exp["id"])
    assert saved["propose_meta"]["cost_usd"] == 0.001
    assert saved["propose_meta"]["tokens"] == 20
    assert saved["total"] == {"cost_usd": 0.002, "tokens": 30}
    store.close()


def test_compose_usage_is_recorded_on_invalid_overlay(tmp_path, monkeypatch):
    rt = _Runtime()
    store = EvalStore(tmp_path / "eval.db")
    exp = hx.create_experiment(store, _case(), reps=1)

    async def fake_case(view, case, store, **kwargs):
        label = kwargs["variant"]["label"].rsplit("-", 1)[-1]
        return _measured_row(label)

    async def editor(cfg, alias, messages, **kwargs):
        if "Propose TWO" in messages[0]["content"]:
            content = json.dumps({
                "invariant": {"rationale": "verify", "overlay": {
                    "prompt_addition": "verify"}},
                "domain": {"rationale": "check", "overlay": {
                    "prompt_addition": "check"}}})
        else:
            content = json.dumps({"rationale": "unprobed tool", "overlay": {
                "tool_description": {"name": "web.search", "content": "new"}}})
        return _editor_result(content)

    monkeypatch.setattr(eval_runner, "run_case", fake_case)
    monkeypatch.setattr(eval_runner, "_model_text", editor)
    result = run(hx.run_experiment(rt, _case(), store, exp["id"]))
    assert result["status"] == "error" and "was not probed" in result["error"]
    saved = store.get_harness_experiment(exp["id"])
    assert saved["compose_meta"]["cost_usd"] == 0.001
    assert saved["compose_meta"]["tokens"] == 20
    assert saved["total"] == {"cost_usd": 0.005, "tokens": 70}
    assert saved["composed"]["runs"] == []
    store.close()


def test_editor_output_is_clamped_to_remaining_shared_cap(tmp_path, monkeypatch):
    rt = _Runtime()
    rt.config["eval"] = {"suite_max_cost_usd": 0.01, "judge_model": "editor"}
    rt.config["costs"] = {"editor": {"input": 0, "output": 100}}
    store = EvalStore(tmp_path / "eval.db")
    exp = hx.create_experiment(store, _case(), reps=1)
    seen = []

    async def fake_case(view, case, store, **kwargs):
        return _measured_row(cost_usd=0, tokens=1)

    async def editor(cfg, alias, messages, **kwargs):
        seen.append(kwargs["max_tokens"])
        return _editor_result("malformed")

    monkeypatch.setattr(eval_runner, "run_case", fake_case)
    monkeypatch.setattr(eval_runner, "_model_text", editor)
    result = run(hx.run_experiment(rt, _case(), store, exp["id"]))
    assert result["status"] == "error"
    assert seen == [100]
    assert store.get_harness_experiment(exp["id"])["total"]["cost_usd"] <= 0.01
    store.close()


def test_experiment_judge_uses_remaining_cost_budget(tmp_path, monkeypatch):
    rt = _Runtime()
    rt.config["eval"] = {"judge_model": "judge"}
    rt.config["costs"] = {"judge": {"input": 0, "output": 100}}
    budget = {"remaining": 0.01}
    seen = []

    async def judge(cfg, alias, messages, **kwargs):
        seen.append(kwargs["max_tokens"])
        result = _editor_result(
            '{"pass":true,"score":9,"notes":"ok","classification":"none"}')
        result.update(cost_usd=0.004, tokens=7)
        return result

    monkeypatch.setattr(eval_runner, "_model_text", judge)
    store = EvalStore(tmp_path / "eval.db")
    row = run(eval_runner.run_case(
        rt, _case(), store, record=False,
        variant={"label": "experiment", "experiment_overlay": {},
                 "experiment_cost_budget": budget}))
    assert row["passed"] is True and row["cost_usd"] == 0.004
    assert seen == [100]
    assert budget["remaining"] == pytest.approx(0.006)
    store.close()


def test_model_budget_uses_prompt_and_output_estimates():
    cfg = {"costs": {"m": {"input": 1, "output": 2}}}
    messages = [{"role": "user", "content": "x" * 100}]
    assert eval_runner._fit_model_budget(
        cfg, "m", messages, 12000, {"remaining": 0.0002}) == 46
    assert eval_runner._fit_model_budget(
        cfg, "m", messages, 12000, {"remaining": 0.0001}) == 0


def test_artifact_drift_aborts_between_repetitions(tmp_path, monkeypatch):
    rt = _Runtime()
    store = EvalStore(tmp_path / "eval.db")
    exp = hx.create_experiment(store, _case(), reps=2)
    calls = []
    editor_calls = []

    async def fake_case(view, case, store, **kwargs):
        calls.append(1)
        rt.system_prompt = "ADMIN EDIT DURING EXPERIMENT"
        return _measured_row()

    async def editor(*args, **kwargs):
        editor_calls.append(1)
        return _editor_result("malformed")

    monkeypatch.setattr(eval_runner, "run_case", fake_case)
    monkeypatch.setattr(eval_runner, "_model_text", editor)
    result = run(hx.run_experiment(rt, _case(), store, exp["id"]))
    assert result["status"] == "error" and "artifacts changed" in result["error"]
    assert len(calls) == 1 and not editor_calls
    saved = store.get_harness_experiment(exp["id"])
    assert len(saved["baseline"]["runs"]) == 1
    assert saved["total"]["cost_usd"] == 0.001
    store.close()


def test_propose_uses_frozen_baseline_artifacts(monkeypatch):
    rt = _Runtime()
    seen = {}

    async def editor(cfg, alias, messages, **kwargs):
        seen["user"] = messages[1]["content"]
        return _editor_result(json.dumps({
            "invariant": {"rationale": "verify", "overlay": {
                "prompt_addition": "verify"}},
            "domain": {"rationale": "check", "overlay": {
                "prompt_addition": "check"}}}))

    monkeypatch.setattr(eval_runner, "_model_text", editor)
    run(hx.propose(rt, _case(), [], [], artifact_snapshot="FROZEN-BASELINE"))
    assert "## Current artifacts\nFROZEN-BASELINE\n" in seen["user"]


def test_candidate_skill_state_requires_trace_evidence(tmp_path, monkeypatch):
    rt = _Runtime()
    skills = tmp_path / "skills"
    skill = skills / "coding"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: coding\ndescription: code\n---\nORIGINAL", encoding="utf-8")
    db = tmp_path / "trace.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " run_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL,"
                " iteration INTEGER, payload_json TEXT)")
    con.commit()
    con.close()
    monkeypatch.setattr(paths, "TRACE_DB", db)
    monkeypatch.setattr(paths, "SKILLS_DIR", skills)
    monkeypatch.setattr(paths, "CUSTOM_SKILLS_DIR", tmp_path / "custom")
    skills_cache_clear()
    states = []

    async def fake_run(message, **kwargs):
        return {"run_id": "fake-run", "status": "ok", "answer": "done",
                "trajectory": "skill.load(coding)→ok", "tools_used": ["skill.load"],
                "budget": {"cost_usd": 0, "tokens": {"total": 5}}}

    async def judge(cfg, ecfg, case, transcript, failures, state):
        states.append(state)
        return {"pass": True, "score": 9, "notes": "fine",
                "judge_model": "j", "cost_usd": 0, "tokens": 1, "error": None,
                "classification": "none", "target": "", "proposed_content": "",
                "what": "", "cause": "", "fix": ""}

    rt.run = fake_run
    monkeypatch.setattr(eval_runner, "_judge", judge)
    store = EvalStore(tmp_path / "eval.db")
    overlay = {"skill_addition": {"name": "coding", "content": "CANDIDATE"}}
    variant = {"label": "experiment", "experiment_overlay": overlay}
    try:
        run(eval_runner.run_case(rt, _case(), store, record=False, variant=variant))
        assert states[-1]["skill_bodies"] == {}
        con = sqlite3.connect(db)
        con.execute("INSERT INTO events (run_id, ts, kind, payload_json)"
                    " VALUES ('fake-run', 0, 'procedure_autoload', ?)",
                    (json.dumps({"skill": "coding"}),))
        con.commit()
        con.close()
        run(eval_runner.run_case(rt, _case(), store, record=False, variant=variant))
        body = states[-1]["skill_bodies"]["coding"]
        assert body.startswith("ORIGINAL") and body.endswith(
            "— Experiment addition —\nCANDIDATE")
    finally:
        store.close()
        skills_cache_clear()


@pytest.mark.parametrize("changes", [
    {"skipped": True}, {"status": "invalid"}, {"status": "crashed"},
    {"judge_error": "bad judge json"}, {"score": None},
])
def test_unmeasured_baseline_stops_before_propose(
        tmp_path, monkeypatch, changes):
    rt = _Runtime()
    store = EvalStore(tmp_path / "eval.db")
    exp = hx.create_experiment(store, _case(), reps=1)
    editor_calls = []

    async def fake_case(view, case, store, **kwargs):
        return _measured_row(**changes)

    async def editor(*args, **kwargs):
        editor_calls.append(1)
        return _editor_result("malformed")

    monkeypatch.setattr(eval_runner, "run_case", fake_case)
    monkeypatch.setattr(eval_runner, "_model_text", editor)
    result = run(hx.run_experiment(rt, _case(), store, exp["id"]))
    assert result["status"] == "error" and "measured evidence" in result["error"]
    assert not editor_calls
    summary = store.get_harness_experiment(exp["id"])["baseline"]["summary"]
    assert summary["counted"] == 0 and summary["measured"] == 0
    store.close()


@pytest.mark.parametrize("bad_arm", ["baseline", "invariant", "domain", "composed"])
def test_create_proposals_independently_requires_every_measured_arm(
        tmp_path, bad_arm):
    store = EvalStore(tmp_path / "eval.db")
    exp = hx.create_experiment(store, _case(), reps=1)
    exp["baseline"]["runs"] = [_measured_row("baseline")]
    for name in ("invariant", "domain"):
        exp["candidates"][name] = {
            "rationale": name, "overlay": {}, "runs": [_measured_row(name)]}
    exp["composed"].update({
        "rationale": "composed", "overlay": {"prompt_addition": "verify"},
        "runs": [_measured_row("composed")]})
    arms = {"baseline": exp["baseline"], "composed": exp["composed"],
            **exp["candidates"]}
    for arm in arms.values():
        arm["summary"] = {"counted": 1, "measured": 1, "pass_rate": 1}
    arms[bad_arm]["runs"] = [_measured_row(bad_arm, judge_error="judge unavailable")]
    exp["status"] = "done"
    store.update_harness_experiment(exp)
    with pytest.raises(ValueError, match=f"{bad_arm} arm lacks usable measured"):
        hx.create_proposals(store, exp["id"])
    assert store.proposals() == []
    store.close()
