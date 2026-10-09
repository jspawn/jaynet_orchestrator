"""procedure.* — saved, re-runnable workspace scripts (the reasonlet pattern).

save writes procedures/<name>.<ext> + a JSON sidecar into the work_root,
run executes it through the code.check engine with inputs injected as ARGS
(python) / $PROC_ARGS (bash), list browses the sidecars.
"""
from __future__ import annotations

import pytest
from conftest import run

from tools.procedure.procedure import ProcedureList, ProcedureRun, ProcedureSave


@pytest.fixture(autouse=True)
def _no_firejail(monkeypatch):
    # The firejail python-sandbox can't enter its private workdir under
    # pytest's pinned tmp data dir — force the direct fallback so procedures
    # really execute (same pattern as test_code_exec_coverage).
    monkeypatch.setattr("tools.code.run.shutil.which", lambda name: None)

FV_CODE = (
    "p = ARGS['monthly']\n"
    "r = ARGS.get('rate', 0.06) / 12\n"
    "n = int(ARGS.get('years', 13) * 12)\n"
    "print(round(p * ((1 + r) ** n - 1) / r, 2))\n"
)


def _save(ctx, name="fv", code=FV_CODE, language="python"):
    return run(ProcedureSave().execute(
        {"name": name, "description": "future value of monthly deposits",
         "code": code, "language": language}, ctx))


def test_save_and_run_python(project, ctx):
    c = ctx(work_root=str(project))
    r = _save(c)
    assert r.status == "ok" and r.result["saved"] == "fv"
    assert (project / "procedures" / "fv.py").exists()
    assert (project / "procedures" / "fv.json").exists()

    r = run(ProcedureRun().execute(
        {"name": "fv", "args": {"monthly": 21, "rate": 0.06, "years": 13}}, c))
    assert r.status == "ok", r.error
    assert r.tool_name == "procedure.run"
    assert r.result["procedure"] == "fv"
    assert "4944.39" in r.result["stdout"]


def test_run_bash_with_proc_args(project, ctx):
    c = ctx(work_root=str(project))
    r = _save(c, name="upper", code='echo "$PROC_ARGS" | python3 -c '
                                    '"import json,sys; print(json.load(sys.stdin)[\'s\'].upper())"',
            language="bash")
    assert r.status == "ok"
    r = run(ProcedureRun().execute({"name": "upper", "args": {"s": "hello"}}, c))
    assert r.status == "ok" and "HELLO" in r.result["stdout"]


def test_save_replaces_and_keeps_created(project, ctx):
    c = ctx(work_root=str(project))
    assert _save(c).result["replaced"] is False
    r = _save(c, code="print('v2')\n")
    assert r.result["replaced"] is True
    r = run(ProcedureRun().execute({"name": "fv",
                                    "args": {"monthly": 1, "years": 1}}, c))
    assert "v2" in r.result["stdout"]


def test_list(project, ctx):
    c = ctx(work_root=str(project))
    assert run(ProcedureList().execute({}, c)).result["count"] == 0
    _save(c)
    r = run(ProcedureList().execute({}, c))
    assert r.result["count"] == 1
    assert r.result["procedures"][0]["name"] == "fv"
    assert "future value" in r.result["procedures"][0]["description"]


def test_save_rejects_bad_name(project, ctx):
    r = _save(ctx(work_root=str(project)), name="Bad Name!")
    assert r.status == "error" and "slug" in r.error


def test_run_unknown_procedure(project, ctx):
    r = run(ProcedureRun().execute({"name": "nope"}, ctx(work_root=str(project))))
    assert r.status == "error" and "no procedure" in r.error


def test_run_rejects_traversal_name(project, ctx):
    """Audit 2026-10-10: run skipped the slug check — '../x' read files
    outside the workspace via the sidecar path. Same rule as save."""
    c = ctx(work_root=str(project))
    for bad in ("../secret", "..", "a/b", "x;rm", ".hidden"):
        r = run(ProcedureRun().execute({"name": bad}, c))
        assert r.status == "error" and "slug" in r.error, bad


def test_save_requires_confirmation():
    """save writes .py/.sh into the workspace — same confirmation policy as
    fs.write/code.patch (it bypassed the write gates; audit 2026-10-10)."""
    assert ProcedureSave().requires_confirmation is True


def test_procedures_need_work_root(ctx):
    r = _save(ctx())
    assert r.status == "error" and "work_root" in r.error


def test_run_failing_procedure_is_ok_signal(project, ctx):
    c = ctx(work_root=str(project))
    _save(c, name="boom", code="import sys; print('nope'); sys.exit(3)\n")
    r = run(ProcedureRun().execute({"name": "boom"}, c))
    # Same contract as code.check: a non-zero exit is data, not a tool error.
    assert r.status == "ok" and r.result["exit_code"] != 0
    assert "nope" in r.result["stdout"]
