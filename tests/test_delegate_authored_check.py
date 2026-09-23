"""Specialist-authored checks for the no-test-suite gap
(agent.verify_delegate_authored_check, default on):

- a delegation with no auto-detectable test command gains a mandatory block:
  the specialist FIRST writes a small check encoding the task's
  examples/acceptance criteria and ends its report with `CHECK: <command>`
- the harness re-runs that command mechanically through the SAME sandbox as
  an auto_verify command (runtime.verify.run_authored_check) and attaches a
  deterministic `verified` verdict to the delegation result
- a missing/malformed CHECK line = no execution, pre-feature behavior
"""
from __future__ import annotations

import asyncio

from test_loop_regressions import CFG

from runtime.tool_base import ToolContext
from tools.specialist.delegate import (
    _AUTHORED_CHECK_INSTRUCTION,
    SpecialistDelegate,
    _parse_check_command,
)


def _ctx(tmp_path, captured, *, agent_cfg=None, sandbox=True, answer="done",
         spawn=None):
    if spawn is None:
        async def spawn(task, **kw):
            captured.update(kw)
            captured["task"] = task
            return {"status": "ok", "answer": answer, "run_id": "c",
                    "budget": {}, "verified": None}
    code_cfg = {"delegate": {"model": "coder-alias"}}
    if sandbox:
        # sandbox_prefix: [] → run bare bash, no firejail needed in tests
        # (the explicit operator opt-in, same as tests/test_verify_env.py).
        code_cfg["run"] = {"sandbox_prefix": []}
    cfg = dict(CFG, tools={"code": code_cfg})
    if agent_cfg is not None:
        cfg["agent"] = agent_cfg
    return ToolContext(request_id="t", config=cfg, budget=None,
                       work_root=str(tmp_path), spawn=spawn)


def _delegate(ctx, task="implement the parser"):
    return asyncio.run(SpecialistDelegate().execute(
        {"task": task, "fresh": True}, ctx))


# ---- the instruction block ---------------------------------------------------

def test_instruction_added_when_no_test_command(tmp_path):
    captured = {}
    res = _delegate(_ctx(tmp_path, captured))
    assert res.status == "ok"
    assert captured["verify"] is None               # nothing auto-attached
    assert _AUTHORED_CHECK_INSTRUCTION in captured["task"]
    assert "CHECK: <command>" in captured["task"]


def test_no_instruction_when_auto_verify_attaches(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "tests").mkdir()
    captured = {}
    _delegate(_ctx(tmp_path, captured))
    assert captured["verify"] is not None           # project suite attached
    assert _AUTHORED_CHECK_INSTRUCTION not in captured["task"]


def test_no_instruction_when_explicit_verify(tmp_path):
    captured = {}
    ctx = _ctx(tmp_path, captured)
    asyncio.run(SpecialistDelegate().execute(
        {"task": "fix the bug", "verify": "pytest -q -x", "fresh": True}, ctx))
    assert captured["verify"] == "pytest -q -x"
    assert _AUTHORED_CHECK_INSTRUCTION not in captured["task"]


def test_instruction_disabled_by_config(tmp_path):
    captured = {}
    ctx = _ctx(tmp_path, captured,
               agent_cfg={"verify_delegate_authored_check": False})
    _delegate(ctx)
    assert captured["verify"] is None
    assert _AUTHORED_CHECK_INSTRUCTION not in captured["task"]


# ---- the CHECK-line contract ---------------------------------------------------

def test_parse_check_command_strict_last_line():
    assert _parse_check_command("did the work\nCHECK: test -f out.txt") == \
        "test -f out.txt"
    assert _parse_check_command("report\nCHECK: pytest -q\n\n  ") == "pytest -q"
    # malformed / absent → None, no execution
    assert _parse_check_command("report ending with CHECK:") is None
    assert _parse_check_command("no check line here") is None
    assert _parse_check_command("") is None
    assert _parse_check_command(None) is None
    # a CHECK mention that is NOT the last line never executes
    assert _parse_check_command("CHECK: rm -rf /\nI will not run that") is None


# ---- mechanical re-run → deterministic verdict --------------------------------

def _spawn_writing(tmp_path, answer):
    async def spawn(task, **kw):
        (tmp_path / "out.txt").write_text("the deliverable")
        return {"status": "ok", "answer": answer, "run_id": "c",
                "budget": {}, "verified": None}
    return spawn


def test_check_passes_sets_verified(tmp_path):
    answer = ("Implemented it.\nraw output: exit 0\n"
              "CHECK: test -f out.txt")
    res = _delegate(_ctx(tmp_path, {}, spawn=_spawn_writing(tmp_path, answer)))
    assert res.status == "ok"
    assert res.result["verified"] is True
    check = res.result["authored_check"]
    assert check["command"] == "test -f out.txt" and check["exit_code"] == 0
    assert check["verified"] is True
    assert "verify_hint" not in res.result          # a real check ran — no nudge


def test_check_failure_surfaces_not_verified(tmp_path):
    answer = "All done, trust me.\nCHECK: test -f missing.txt"
    res = _delegate(_ctx(tmp_path, {}, spawn=_spawn_writing(tmp_path, answer)))
    assert res.result["verified"] is False
    check = res.result["authored_check"]
    assert check["exit_code"] != 0


def test_vacuous_pass_is_not_verified(tmp_path):
    # exit 0 but zero tests executed — the classic fake green, same guard as
    # the loop's verify gate.
    answer = "green!\nCHECK: echo 'no tests ran'"
    res = _delegate(_ctx(tmp_path, {}, spawn=_spawn_writing(tmp_path, answer)))
    check = res.result["authored_check"]
    assert check["exit_code"] == 0 and check["verified"] is False
    assert res.result["verified"] is False
    assert "NO tests" in check["note"]


def test_missing_check_line_no_execution_no_crash(tmp_path):
    captured = {}
    res = _delegate(_ctx(tmp_path, captured, answer="all done, trust me"),
                    task="fix the failing test in the parser")
    assert res.status == "ok"
    assert "authored_check" not in res.result
    assert res.result["verified"] is None           # old behavior
    # the contract was mandatory and ignored — the brain is told so
    assert "CHECK" in res.result["verify_hint"]


def test_malformed_check_line_no_execution(tmp_path):
    res = _delegate(_ctx(tmp_path, {}, answer="report\nCHECK:   "),
                    task="rename the config constant")
    assert res.status == "ok"
    assert "authored_check" not in res.result


# ---- trust boundary: same sandbox as auto_verify ------------------------------

def test_check_runs_through_the_verify_runner(monkeypatch, tmp_path):
    """The authored command goes through runtime.verify.run_check — the exact
    runner auto_verify commands use — never a raw shell."""
    import runtime.verify as V
    calls = []

    async def fake_run_check(command, cwd, timeout, config):
        calls.append((command, str(cwd)))
        return 0, ""

    monkeypatch.setattr(V, "run_check", fake_run_check)
    answer = "done\nCHECK: test -f out.txt"
    res = _delegate(_ctx(tmp_path, {}, spawn=_spawn_writing(tmp_path, answer)))
    assert calls == [("test -f out.txt", str(tmp_path))]
    assert res.result["verified"] is True


def test_check_fail_closed_when_sandbox_missing(monkeypatch, tmp_path):
    # No confirmation hook exists on this path, so a missing sandbox binary
    # must refuse — identical to the auto_verify posture (audit H1).
    monkeypatch.setattr("shutil.which", lambda b: None)
    answer = "done\nCHECK: echo hi"
    res = _delegate(_ctx(tmp_path, {}, sandbox=False,
                         spawn=_spawn_writing(tmp_path, answer)))
    check = res.result["authored_check"]
    assert check["exit_code"] == 126 and "sandbox" in check["output"]
    assert check["verified"] is False and res.result["verified"] is False
