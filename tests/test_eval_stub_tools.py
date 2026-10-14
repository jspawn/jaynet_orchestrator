"""Stub-tool eval cases (BFCL-style): registration, restriction, grading,
judge-off — driven against the fake-runtime harness like test_eval_harness."""
from __future__ import annotations

import json
import sqlite3

import pytest
from conftest import run

from runtime import eval_runner, paths
from runtime.eval_cases import EvalCase, parse_case, validate_case_dict
from runtime.eval_store import EvalStore


class _DictRegistry:
    """Registry double with the two methods stub cases exercise."""

    def __init__(self, names=()):
        self._tools = {n: object() for n in names}

    def all(self):
        return list(self._tools.values())

    def register_instance(self, tool):
        if tool.name in self._tools:
            return False
        self._tools[tool.name] = tool
        return True

    def unregister(self, name):
        return self._tools.pop(name, None) is not None


class _FakeTool:
    def __init__(self, name):
        self.name = name
        self.requires_confirmation = False


class _StubRuntime:
    def __init__(self):
        self.config = {"eval": {"judge_temperature": 0.0, "max_cost_usd": 1.0},
                       "privacy": {}, "costs": {}}
        self.registry = _DictRegistry()
        self.registry._tools = {"web.search": _FakeTool("web.search"),
                                "eval.run": _FakeTool("eval.run")}
        self.model = "fake-brain"
        self.calls = []

    async def run(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return {"run_id": f"fake-{len(self.calls)}", "status": "ok",
                "answer": "done", "trajectory": "bfcl.calc(x)→ok",
                "tools_used": ["bfcl.calc"],
                "budget": {"iterations": 1, "cost_usd": 0.0,
                           "tokens": {"total": 5}}}


def _trace_db(tmp_path, rows):
    db = tmp_path / "trace.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " run_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL,"
                " iteration INTEGER, payload_json TEXT)")
    for rid, payload in rows:
        con.execute("INSERT INTO events (run_id, ts, kind, payload_json)"
                    " VALUES (?, 0, 'tool_result', ?)",
                    (rid, json.dumps(payload)))
    con.commit()
    con.close()
    return db


_STUB = [{"name": "bfcl.calc", "description": "area calc",
          "parameters": {"type": "object",
                         "properties": {"base": {"type": "integer"},
                                        "height": {"type": "integer"}},
                         "required": ["base", "height"]}}]


def _bfcl_case(**kw):
    base = dict(id="bfcl-demo", name="BFCL demo", turns=["area?"],
                expect={"tool_calls": [{"bfcl.calc": {"base": [10],
                                                      "height": [5]}}]},
                judge=False, judge_rubric="",
                stub_tools=list(_STUB))
    base.update(kw)
    return EvalCase(**base)


# ---- validation ---------------------------------------------------------------

def test_validate_judge_off_needs_no_rubric():
    import yaml
    raw = yaml.safe_load(
        "name: t\nturns: [{user: hi}]\njudge: false\n"
        "stub_tools: [{name: bfcl.x}]\nexpect: {tool_calls: []}\n")
    assert validate_case_dict("t", raw) == []
    c = parse_case("t", "name: t\nturns: [{user: hi}]\njudge: off\n"
                        "expect: {tool_calls: []}\n", "custom")
    assert c.judge is False and c.judge_rubric == ""


@pytest.mark.parametrize("snippet,needle", [
    ("judge: nope", "judge must be a boolean"),
    ("judge: false\nstub_tools: [{'name': 'bad name'}]",
     "invalid stub tool name"),
    ("judge: false\nexpect: {tool_calls: [{a: 1, b: 2}]}",
     "single-key mappings"),
    ("judge: false\nexpect: {tool_calls: 'x'}", "expect.tool_calls"),
    ("judge: false\nstub_tool: []", "unknown case key 'stub_tool'"),
])
def test_validate_stub_errors(snippet, needle):
    import yaml
    raw = yaml.safe_load(f"name: t\nturns: [{{user: hi}}]\n{snippet}\n")
    errors = validate_case_dict("t", raw)
    assert any(needle in e for e in errors), errors


# ---- grading from the trace ---------------------------------------------------

def test_tool_call_grades_match_and_mismatch(tmp_path, monkeypatch):
    db = _trace_db(tmp_path, [
        ("r1", {"tool": "bfcl.calc", "status": "ok",
                "args": {"base": 10, "height": 5}}),
        ("r1", {"tool": "web.search", "status": "ok",     # not a stub: ignored
                "args": {"q": "x"}}),
    ])
    monkeypatch.setattr(paths, "TRACE_DB", db)
    gt = [{"bfcl.calc": {"base": [10], "height": [5]}}]
    assert eval_runner._tool_call_grades(["r1"], ["bfcl.calc"], gt) == []
    fails = eval_runner._tool_call_grades(
        ["r1"], ["bfcl.calc"], [{"bfcl.calc": {"base": [11], "height": [5]}}])
    assert fails and "base" in fails[0]


def test_tool_call_grades_irrelevance(tmp_path, monkeypatch):
    db = _trace_db(tmp_path, [
        ("r1", {"tool": "bfcl.calc", "status": "error",   # even failed = attempt
                "args": {"base": 10}}),
    ])
    monkeypatch.setattr(paths, "TRACE_DB", db)
    assert eval_runner._tool_call_grades(["r2"], ["bfcl.calc"], []) == []
    fails = eval_runner._tool_call_grades(["r1"], ["bfcl.calc"], [])
    assert fails and "irrelevance" in fails[0]


def test_tool_call_grades_ignores_non_ok_for_matching(tmp_path, monkeypatch):
    db = _trace_db(tmp_path, [
        ("r1", {"tool": "bfcl.calc", "status": "error",
                "args": {"base": 10, "height": 5}}),
    ])
    monkeypatch.setattr(paths, "TRACE_DB", db)
    gt = [{"bfcl.calc": {"base": [10], "height": [5]}}]
    fails = eval_runner._tool_call_grades(["r1"], ["bfcl.calc"], gt)
    assert fails and "expected exactly 1 call(s), got 0" in fails[0]


def test_tool_call_grades_first_turn_only(tmp_path, monkeypatch):
    """BFCL is a single-turn protocol: calls after the second model_start
    (retry loops against stub payloads) must NOT pollute grading."""
    db = tmp_path / "trace.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " run_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL,"
                " iteration INTEGER, payload_json TEXT)")
    rows = [
        (1, "model_start", {}),
        (2, "tool_result", {"tool": "bfcl.calc", "status": "ok",
                            "args": {"base": 10, "height": 5}}),
        (3, "model_start", {}),
        # retry-loop noise AFTER the first turn — must be ignored:
        (4, "tool_result", {"tool": "bfcl.calc", "status": "ok",
                            "args": {"base": 99, "height": 99}}),
        (5, "tool_result", {"tool": "bfcl.calc", "status": "ok",
                            "args": {"base": 1, "height": 1}}),
    ]
    for ts, kind, payload in rows:
        con.execute("INSERT INTO events (run_id, ts, kind, payload_json)"
                    " VALUES ('r1', ?, ?, ?)", (ts, kind, json.dumps(payload)))
    con.commit(); con.close()
    monkeypatch.setattr(paths, "TRACE_DB", db)
    gt = [{"bfcl.calc": {"base": [10], "height": [5]}}]
    assert eval_runner._tool_call_grades(["r1"], ["bfcl.calc"], gt) == []


# ---- end to end through run_case ----------------------------------------------

def test_run_case_stub_tools_restricts_and_grades(tmp_path, monkeypatch):
    rt = _StubRuntime()
    db = _trace_db(tmp_path, [
        ("fake-1", {"tool": "bfcl.calc", "status": "ok",
                    "args": {"base": 10, "height": 5}}),
    ])
    monkeypatch.setattr(paths, "TRACE_DB", db)
    store = EvalStore(tmp_path / "eval.db")
    row = run(eval_runner.run_case(rt, _bfcl_case(), store))
    assert row["passed"] in (True, 1)
    assert row["judge_model"] == "none (judge: off)"
    # run saw ONLY the stub tool (BFCL single-turn condition)
    assert rt.calls[0][1]["tools"] == ["bfcl.calc"]
    # and the stub was unregistered afterwards
    assert "bfcl.calc" not in [t.name for t in rt.registry.all()]


def test_run_case_stub_tools_mismatch_fails(tmp_path, monkeypatch):
    rt = _StubRuntime()
    db = _trace_db(tmp_path, [
        ("fake-1", {"tool": "bfcl.calc", "status": "ok",
                    "args": {"base": 99, "height": 5}}),
    ])
    monkeypatch.setattr(paths, "TRACE_DB", db)
    store = EvalStore(tmp_path / "eval.db")
    row = run(eval_runner.run_case(rt, _bfcl_case(), store))
    assert row["passed"] in (False, 0)
    assert "deterministic checks only" in row["judge_notes"]
    assert "base" in row["judge_notes"]


def test_run_case_stub_name_collision_raises(tmp_path):
    rt = _StubRuntime()
    case = _bfcl_case(stub_tools=[{"name": "web.search"}])
    store = EvalStore(tmp_path / "eval.db")
    with pytest.raises(ValueError, match="collides"):
        run(eval_runner.run_case(rt, case, store))


def test_run_case_stub_unregistered_when_case_skips(tmp_path):
    """A skip AFTER stub registration (requires_tools miss, container
    preflight, a setup exception) must not leak the stub into the live
    registry — a leaked stub answers real calls in later cases and chats
    with its canned payload (code audit 2026-10-14)."""
    rt = _StubRuntime()
    case = _bfcl_case(requires_tools=["not.there"])
    store = EvalStore(tmp_path / "eval.db")
    row = run(eval_runner.run_case(rt, case, store))
    assert row["skipped"] is True
    assert "bfcl.calc" not in [t.name for t in rt.registry.all()]
    # …and re-running the same case doesn't die on a phantom name collision.
    row2 = run(eval_runner.run_case(rt, case, store))
    assert row2["skipped"] is True
