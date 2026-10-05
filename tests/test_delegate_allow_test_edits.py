"""Caller-declared tamper exceptions (specialist.delegate allow_test_edits):
the CALLING brain — never the child — names relative test/check paths the
TASK legitimately modifies (live: "adjust the test so it checks the new
name" — the specialist edited test_service.py exactly as instructed and
the verify gate's tamper protection killed the delegation as "not
converging"). The declaration lands as verify.unprotect on BOTH the
explicit-verify and the auto_verify path; absent or mis-shaped, the verify
dict is exactly what it was before. Fake ctx.spawn captures the verify
dict the child would run under."""
from __future__ import annotations

import asyncio

from runtime.tool_base import ToolContext
from tests.test_loop_regressions import CFG
from tools.specialist.delegate import SpecialistDelegate


def _ctx(tmp_path, captured):
    async def spawn(task, **kw):
        captured.update(kw)
        captured["task"] = task
        return {"status": "ok", "answer": "done", "run_id": "c",
                "budget": {}, "verified": None}
    # sandbox_prefix: [] → run bare bash, no firejail needed in tests (the
    # explicit operator opt-in, same as tests/test_verify_env.py).
    cfg = dict(CFG, tools={"code": {"delegate": {"model": "coder-alias"},
                                    "run": {"sandbox_prefix": []}}})
    return ToolContext(request_id="t", config=cfg, budget=None,
                       work_root=str(tmp_path), spawn=spawn)


def _delegate(ctx, **args):
    return asyncio.run(SpecialistDelegate().execute(
        {"task": "rename the setting and adjust the test", "fresh": True,
         **args}, ctx))


def test_explicit_verify_string_gains_unprotect(tmp_path):
    captured = {}
    res = _delegate(_ctx(tmp_path, captured), verify="pytest -q -x",
                    allow_test_edits=["test_service.py"])
    assert res.status == "ok"
    assert captured["verify"] == {"command": "pytest -q -x",
                                  "unprotect": ["test_service.py"]}


def test_explicit_verify_dict_gains_unprotect(tmp_path):
    captured = {}
    res = _delegate(_ctx(tmp_path, captured),
                    verify={"command": "pytest -q", "max_checks": 2},
                    allow_test_edits=["test_service.py", "tests/test_api.py"])
    assert res.status == "ok"
    assert captured["verify"]["unprotect"] == ["test_service.py",
                                               "tests/test_api.py"]
    assert captured["verify"]["max_checks"] == 2        # rest untouched


def test_auto_verify_gains_unprotect(tmp_path):
    """No explicit verify: the workspace's auto-detected suite is attached
    (auto_verify) and the declaration rides on that too."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "tests").mkdir()
    captured = {}
    res = _delegate(_ctx(tmp_path, captured),
                    allow_test_edits=["tests/test_service.py"])
    assert res.status == "ok"
    assert captured["verify"] is not None               # suite auto-attached
    assert captured["verify"]["unprotect"] == ["tests/test_service.py"]


def test_absent_allow_test_edits_no_unprotect_key(tmp_path):
    captured = {}
    res = _delegate(_ctx(tmp_path, captured), verify="pytest -q -x")
    assert res.status == "ok"
    assert captured["verify"] == "pytest -q -x"         # passes through raw


def test_misshaped_allow_test_edits_ignored(tmp_path):
    """A non-list value must never widen the exception — ignored entirely."""
    captured = {}
    res = _delegate(_ctx(tmp_path, captured), verify="pytest -q -x",
                    allow_test_edits="test_service.py")
    assert res.status == "ok"
    assert captured["verify"] == "pytest -q -x"


def test_allow_test_edits_without_verify_is_inert(tmp_path):
    """No verify at all (authored-check path): there is no tamper baseline
    to exempt from, so the declaration changes nothing."""
    captured = {}
    res = _delegate(_ctx(tmp_path, captured),
                    allow_test_edits=["test_service.py"])
    assert res.status == "ok"
    assert captured["verify"] is None
