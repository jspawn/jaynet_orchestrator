"""trace.coverage: deterministic self-test outcomes from the trace, not prose."""

import asyncio
import json

import tools.trace.coverage as C
from runtime.tool_base import ToolContext
from runtime.trace import Trace


def _ctx(db, run_id="R1", owner="alice"):
    return ToolContext(request_id=run_id, budget=None, owner=owner,
                       config={"trace": {"db_path": str(db)}})


def _trace(tmp_path, owner="alice"):
    trace = Trace(str(tmp_path / "trace.db"), log_content=True)
    trace.start_run("R1", "test everything", owner=owner)
    return trace


def _result(tool, status="ok", result=None, error=None, args=None):
    return {
        "tool": tool, "args": args or {}, "status": status, "error": error,
        "result_preview": (json.dumps({"status": "ok", "result": result or {}})
                           if status == "ok" else None),
    }


def _run(tool, ctx):
    return asyncio.run(tool.execute({}, ctx))


def _rows(res):
    rows = {}
    for line in res.result["markdown"].splitlines():
        if not line.startswith("| `"):
            continue
        tool = line.split("`", 2)[1]
        parts = line.split("|")
        rows[tool] = {"status": parts[2].strip(), "note": parts[3].strip()}
    return rows


def test_coverage_marks_nested_failures_and_uncalled_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "discover_skills_layered_cached",
                        lambda *a: {"selftest": {}, "audio": {}})
    trace = _trace(tmp_path)
    available = ["fs.read", "fs.write", "code.check", "lint.run", "trace.coverage"]
    trace.log("R1", "tool_selection", 0,
              {"selected": "all", "available": available})
    trace.log("R1", "tool_result", 1, _result("fs.read", result={"text": "ok"}))
    trace.log("R1", "tool_result", 1, _result(
        "code.check", result={"ok": False, "exit_code": 1,
                              "stderr": "SyntaxError: invalid syntax"}))
    trace.log("R1", "tool_result", 1, _result(
        "lint.run", result={"passed": False}))
    trace.close()

    res = _run(C.TraceCoverage(), _ctx(tmp_path / "trace.db"))
    assert res.status == "ok"
    assert res.result["summary"] == {
        "available": 5, "ok": 2, "ok_after_retry": 0,
        "failed": 2, "not_run": 1, "selection_complete": True,
    }
    rows = _rows(res)
    assert rows["fs.read"]["status"] == "ok"
    assert rows["code.check"]["status"] == "failed"
    assert "SyntaxError" in rows["code.check"]["note"]
    assert rows["lint.run"]["status"] == "failed"
    assert "passed=false" in rows["lint.run"]["note"]
    assert rows["fs.write"]["status"] == "not_run"
    assert rows["trace.coverage"]["status"] == "ok"
    assert "| `fs.write` | not_run |" in res.result["markdown"]
    assert "2 failed, 1 not run" in res.result["markdown"]


def test_coverage_reports_retries_and_actual_skill_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "discover_skills_layered_cached",
                        lambda *a: {"selftest": {}, "audio": {}})
    trace = _trace(tmp_path)
    trace.log("R1", "tool_selection", 0, {
        "selected": ["pdf.create", "skill.list", "skill.load", "trace.coverage"]})
    trace.log("R1", "tool_result", 1,
              _result("pdf.create", status="error", error="missing input"))
    trace.log("R1", "tool_result", 2,
              _result("pdf.create", result={"path": "probe.pdf"}))
    trace.log("R1", "tool_result", 2,
              _result("skill.list", result={"skills": []}))
    trace.log("R1", "tool_result", 3,
              _result("skill.load", args={"name": "selftest"}, result={"body": "x"}))
    trace.log("R1", "tool_result", 3,
              _result("skill.load", status="error", args={"name": "docs"},
                      error="no such skill"))
    trace.close()

    res = _run(C.TraceCoverage(), _ctx(tmp_path / "trace.db"))
    rows = _rows(res)
    assert rows["pdf.create"]["status"] == "ok_after_retry"
    assert "missing input" in rows["pdf.create"]["note"]
    skills = res.result["skills"]
    assert skills["discovered"] == 2 and skills["catalog_checked"] is True
    assert skills["loaded_ok"] == ["selftest"]
    assert skills["load_failed"] == {"docs": "no such skill"}
    assert skills["not_loaded"] == ["audio"]
    assert "loading every skill is not required" in skills["note"]


def test_coverage_includes_expanded_tools_and_scopes_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "discover_skills_layered_cached", lambda *a: {})
    trace = _trace(tmp_path)
    trace.log("R1", "tool_selection", 0, {"selected": ["trace.coverage"]})
    trace.log("R1", "tool_selection", 2, {"added": ["web.search"]})
    trace.log("R1", "tool_result", 3, _result("web.search", result=[]))
    trace.close()
    db = tmp_path / "trace.db"

    res = _run(C.TraceCoverage(), _ctx(db))
    assert res.result["summary"]["available"] == 2
    assert res.result["summary"]["not_run"] == 0
    assert res.result["summary"]["selection_complete"] is True

    other = _run(C.TraceCoverage(), _ctx(db, owner="bob"))
    assert other.status == "error" and "no trace events" in other.error
